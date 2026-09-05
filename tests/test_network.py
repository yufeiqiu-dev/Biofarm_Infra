"""Isolation between environments inside the shared VPC.

Staging and production share one VPC to avoid paying for a second NAT. That is a
reasonable trade only for as long as the separation between them is real, and
"real" here means checked. These are the tests that make the topology
defensible; if one of them starts failing, the answer is not to update the test.

The database-level half of the isolation lives in tests/test_data.py, which
asserts that each database accepts traffic only from its own environment.
"""

from __future__ import annotations

import ipaddress

import pytest

from config import ENVIRONMENTS, PROD_SUBNET_GROUP, STAGING_SUBNET_GROUP, VPC_CIDR


@pytest.fixture(scope="module")
def network(templates):
    return templates["Biofarm-Network"]


@pytest.fixture(scope="module")
def subnets(network) -> dict:
    return network.find_resources("AWS::EC2::Subnet")


def _cidr_of(resource) -> str:
    return resource["Properties"]["CidrBlock"]


def _subnets_named(subnets: dict, group: str) -> list[dict]:
    """Subnets carry their group in the Name tag CDK generates."""
    found = []
    for resource in subnets.values():
        tags = {t["Key"]: t.get("Value", "") for t in resource["Properties"].get("Tags", [])}
        name = tags.get("Name", "")
        if isinstance(name, str) and f"/{group}Subnet" in name:
            found.append(resource)
    return found


# --- the subnet groups exist and do not overlap ---

def test_each_environment_has_its_own_private_subnets(subnets):
    """Without separate subnet groups there is no boundary for the NACLs below
    to point at, and isolation would rest on security groups alone."""
    for group in (PROD_SUBNET_GROUP, STAGING_SUBNET_GROUP):
        assert len(_subnets_named(subnets, group)) == 2, f"{group} should span 2 AZs"


def test_the_environment_subnets_do_not_overlap(subnets):
    prod = [ipaddress.ip_network(_cidr_of(s)) for s in _subnets_named(subnets, PROD_SUBNET_GROUP)]
    staging = [
        ipaddress.ip_network(_cidr_of(s)) for s in _subnets_named(subnets, STAGING_SUBNET_GROUP)
    ]
    assert prod and staging

    for p in prod:
        for s in staging:
            assert not p.overlaps(s), f"{p} overlaps {s}"


def test_every_subnet_sits_inside_the_declared_vpc_range(subnets):
    vpc = ipaddress.ip_network(VPC_CIDR)
    for resource in subnets.values():
        assert ipaddress.ip_network(_cidr_of(resource)).subnet_of(vpc)


# --- the NACLs deny cross-environment traffic ---

def test_there_is_one_acl_per_environment(network):
    network.resource_count_is("AWS::EC2::NetworkAcl", len(ENVIRONMENTS))


def test_every_private_subnet_is_associated_with_an_acl(network, subnets):
    """An ACL that is not associated with anything denies nothing. CDK will
    happily synthesize one."""
    associations = network.find_resources("AWS::EC2::SubnetNetworkAclAssociation")
    private_count = sum(
        len(_subnets_named(subnets, g)) for g in (PROD_SUBNET_GROUP, STAGING_SUBNET_GROUP)
    )
    assert len(associations) == private_count


def test_each_acl_denies_the_other_environments_cidrs(network, subnets):
    entries = network.find_resources("AWS::EC2::NetworkAclEntry")

    denies = [
        e["Properties"]
        for e in entries.values()
        if e["Properties"].get("RuleAction") == "deny"
    ]
    assert denies, "no deny rules at all - the environments are not isolated"

    prod_cidrs = {_cidr_of(s) for s in _subnets_named(subnets, PROD_SUBNET_GROUP)}
    staging_cidrs = {_cidr_of(s) for s in _subnets_named(subnets, STAGING_SUBNET_GROUP)}
    denied_cidrs = {d["CidrBlock"] for d in denies}

    # Every subnet of each environment must be denied, in both directions, by
    # the other environment's ACL.
    assert prod_cidrs <= denied_cidrs, f"prod subnets not denied: {prod_cidrs - denied_cidrs}"
    assert staging_cidrs <= denied_cidrs, (
        f"staging subnets not denied: {staging_cidrs - denied_cidrs}"
    )


def test_the_denies_are_evaluated_before_the_catch_all_allow(network):
    """NACL rules are processed in ascending order and stop at the first match.
    A deny numbered above the allow is a deny that never runs."""
    entries = [e["Properties"] for e in network.find_resources("AWS::EC2::NetworkAclEntry").values()]

    lowest_allow = min(
        e["RuleNumber"] for e in entries if e.get("RuleAction") == "allow"
    )
    highest_deny = max(e["RuleNumber"] for e in entries if e.get("RuleAction") == "deny")

    assert highest_deny < lowest_allow


def test_denies_cover_both_directions(network, subnets):
    """NACLs are stateless: a deny on ingress alone still lets this environment
    open a connection outward to the other one."""
    entries = [e["Properties"] for e in network.find_resources("AWS::EC2::NetworkAclEntry").values()]
    denies = [e for e in entries if e.get("RuleAction") == "deny"]

    inbound = {d["CidrBlock"] for d in denies if not d.get("Egress", False)}
    outbound = {d["CidrBlock"] for d in denies if d.get("Egress", False)}

    assert inbound == outbound, "a denied CIDR is blocked in only one direction"
    assert inbound, "no inbound denies"


# --- the NAT instance is not exposed ---

def test_the_nat_does_not_accept_traffic_from_the_internet(network):
    """The construct's default gives the NAT a security group accepting all
    traffic from 0.0.0.0/0, on a host that sits in a public subnet with a public
    IP. `cdk synth` warns about it, which is easy to scroll past."""
    groups = network.find_resources("AWS::EC2::SecurityGroup")
    nat_groups = [g for lid, g in groups.items() if "Nat" in lid]
    assert nat_groups, "expected a NAT security group"

    for group in nat_groups:
        for rule in group["Properties"].get("SecurityGroupIngress", []):
            assert rule.get("CidrIp") != "0.0.0.0/0", rule
            assert rule.get("CidrIp") == VPC_CIDR, rule


# --- cost and egress shape ---

def test_there_is_exactly_one_nat_and_it_is_an_instance(network):
    """A NAT Gateway is roughly $33 a month against roughly $7 for this. One per
    AZ would quietly double whichever it is."""
    network.resource_count_is("AWS::EC2::NatGateway", 0)
    network.resource_count_is("AWS::EC2::Instance", 1)


def test_s3_traffic_avoids_the_nat(network):
    """A gateway endpoint is free, and image deletes and cleanup would otherwise
    be billed as NAT data processing."""
    endpoints = network.find_resources("AWS::EC2::VPCEndpoint")
    services = [e["Properties"]["ServiceName"] for e in endpoints.values()]
    assert any(
        isinstance(s, dict) or "s3" in str(s).lower() for s in services
    ), services
