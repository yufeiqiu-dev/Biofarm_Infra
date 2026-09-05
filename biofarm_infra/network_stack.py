"""One VPC, shared by every environment.

Sharing is a cost decision: a second VPC means a second NAT, and at this traffic
the NAT is a meaningful share of the monthly bill. The consequence is that
network isolation between staging and production has to be built rather than
inherited, because both now sit inside the same address space.

Three things do that work, in order of how much they are relied on:

1. **Separate private subnet groups.** Each environment gets its own subnets.
   Without this, nothing below is expressible - there would be no boundary to
   point at.
2. **Security groups.** Each database accepts traffic only from its own
   environment's application security group. This is the primary control, and
   `tests/test_network.py` fails if it is ever widened.
3. **Network ACLs denying traffic between the two subnet groups.** Subnet-level,
   evaluated before security groups, and independent of them. This exists so
   that a single mistaken security group rule is not the whole story.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

from config import ENVIRONMENTS, PROD_SUBNET_GROUP, STAGING_SUBNET_GROUP, VPC_CIDR

PUBLIC_SUBNET_GROUP = "public"

# Deny rules must be evaluated before the catch-all allow. NACL rules are
# processed in ascending order and stop at the first match, so the numbers here
# are the mechanism, not decoration.
_NACL_RULE_DENY_PEER = 100
_NACL_RULE_ALLOW_REST = 200


class NetworkStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # A NAT instance rather than a NAT Gateway: roughly $7 a month against
        # roughly $33. What you give up is real - a gateway is managed, redundant
        # per AZ, and scales itself, where this is one t4g.nano you are
        # responsible for patching, and it is a single point of failure for all
        # outbound traffic. At this volume that trade is worth making, but it is
        # a trade.
        #
        # default_allowed_traffic is not optional here. Left at its default the
        # construct gives the NAT a security group accepting *all traffic from
        # 0.0.0.0/0* - on an instance that sits in a public subnet with a public
        # IP. `cdk synth` warns about it, and it is the kind of warning that gets
        # scrolled past. OUTBOUND_ONLY closes it, and the rule below reopens
        # exactly the path that is actually needed.
        nat_provider = ec2.NatProvider.instance_v2(
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE4_GRAVITON, ec2.InstanceSize.NANO
            ),
            default_allowed_traffic=ec2.NatTrafficDirection.OUTBOUND_ONLY,
        )

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr(VPC_CIDR),
            max_azs=2,
            nat_gateway_provider=nat_provider,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name=PUBLIC_SUBNET_GROUP,
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                # One private group per environment. Both need egress: the
                # backend calls Stripe, and Cognito's JWKS endpoint is public.
                ec2.SubnetConfiguration(
                    name=PROD_SUBNET_GROUP,
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name=STAGING_SUBNET_GROUP,
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )

        # The one path the NAT actually needs: instances inside this VPC
        # reaching the internet through it. Scoped to the VPC's own CIDR, so the
        # instance ignores the internet talking to it directly.
        nat_provider.connections.allow_from(
            ec2.Peer.ipv4(VPC_CIDR),
            ec2.Port.all_traffic(),
            "Private subnets egress via the NAT instance",
        )

        # Free, and it keeps S3 traffic off the NAT entirely. The backend hands
        # out presigned URLs so the browser uploads directly, but deletes and
        # batch cleanup still go through the application.
        self.vpc.add_gateway_endpoint(
            "S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3
        )

        # Interface endpoints for Secrets Manager and ECR are deliberately not
        # here. They would remove another NAT dependency, but at roughly $7 per
        # endpoint per AZ they cost more than the NAT they would be sparing.

        self._isolate_environments()

    def _isolate_environments(self) -> None:
        """Deny traffic between environments at the subnet level.

        Security groups are the primary control and are asserted in the tests.
        This is the second layer: a NACL is evaluated before any security group
        and is configured independently of them, so widening a security group by
        mistake does not on its own open a path between environments.

        The shape is deliberately simple - deny the peer, allow everything else -
        because NACLs are stateless and a narrower allow rule would mean
        enumerating ephemeral port ranges in both directions, which is where
        NACLs usually start silently breaking things.
        """
        groups = {cfg.name: cfg.subnet_group for cfg in ENVIRONMENTS}

        for env_name, subnet_group in groups.items():
            peers = [g for name, g in groups.items() if name != env_name]

            acl = ec2.NetworkAcl(
                self,
                f"{env_name.capitalize()}Acl",
                vpc=self.vpc,
                subnet_selection=ec2.SubnetSelection(subnet_group_name=subnet_group),
            )

            rule_number = _NACL_RULE_DENY_PEER
            for peer_group in peers:
                for subnet in self.vpc.select_subnets(
                    subnet_group_name=peer_group
                ).subnets:
                    for direction in (
                        ec2.TrafficDirection.INGRESS,
                        ec2.TrafficDirection.EGRESS,
                    ):
                        acl.add_entry(
                            f"Deny{peer_group}{subnet.node.id}{direction.name}",
                            cidr=ec2.AclCidr.ipv4(subnet.ipv4_cidr_block),
                            rule_number=rule_number,
                            traffic=ec2.AclTraffic.all_traffic(),
                            direction=direction,
                            rule_action=ec2.Action.DENY,
                        )
                    rule_number += 1

            for direction in (ec2.TrafficDirection.INGRESS, ec2.TrafficDirection.EGRESS):
                acl.add_entry(
                    f"AllowRest{direction.name}",
                    cidr=ec2.AclCidr.any_ipv4(),
                    rule_number=_NACL_RULE_ALLOW_REST,
                    traffic=ec2.AclTraffic.all_traffic(),
                    direction=direction,
                    rule_action=ec2.Action.ALLOW,
                )

    def private_subnets_for(self, subnet_group: str) -> ec2.SubnetSelection:
        return ec2.SubnetSelection(subnet_group_name=subnet_group)
