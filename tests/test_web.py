"""Frontend hosting.

Everything a branch builds with ends up inside JavaScript the browser downloads,
so the risk here is not leakage of the values themselves - it is values that are
subtly wrong and fail at sign-in rather than at deploy. Every one of them is
taken from a construct so it cannot drift; these assert that.
"""

from __future__ import annotations

import json

import pytest

from config import ENVIRONMENTS


@pytest.fixture(scope="module")
def webs(templates) -> dict:
    return {cfg.name: templates[f"Biofarm-Web-{cfg.name}"] for cfg in ENVIRONMENTS}


def _app(template) -> dict:
    apps = template.find_resources("AWS::Amplify::App")
    assert len(apps) == 1
    return next(iter(apps.values()))["Properties"]


def _branch(template) -> dict:
    branches = template.find_resources("AWS::Amplify::Branch")
    assert len(branches) == 1
    return next(iter(branches.values()))["Properties"]


def _env(template) -> dict:
    return {v["Name"]: v["Value"] for v in _branch(template)["EnvironmentVariables"]}


# --- the GitHub token ---

@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_github_token_is_a_noecho_parameter(webs, env_name):
    """It cannot be an ssm-secure dynamic reference: that pattern works on an
    allowlist of eleven resource properties and Amplify is not among them, so
    CloudFormation would store the reference string verbatim as the token."""
    parameters = webs[env_name].to_json().get("Parameters", {})
    assert "GitHubAccessToken" in parameters
    assert parameters["GitHubAccessToken"]["NoEcho"] is True


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_no_token_value_is_baked_into_the_template(webs, env_name):
    rendered = json.dumps(webs[env_name].to_json())
    for marker in ("ghp_", "github_pat_", "gho_"):
        assert marker not in rendered, marker
    # And it must be the parameter that reaches the resource, not a literal.
    assert _app(webs[env_name])["AccessToken"] == {"Ref": "GitHubAccessToken"}


# --- deep links ---

@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_deep_links_are_rewritten_to_the_app_shell(webs, env_name):
    """Routing is client-side, so /orders/<id> is not a file Amplify can serve.
    Without this rule, refreshing any page below the root 404s - and reloading
    an order page is the first thing anyone does."""
    rules = _app(webs[env_name])["CustomRules"]
    spa = [r for r in rules if r["Target"] == "/index.html"]
    assert spa, rules
    assert spa[0]["Status"] == "200"


# --- branch wiring ---

def test_each_app_builds_its_own_environments_branch(webs):
    for cfg in ENVIRONMENTS:
        assert _branch(webs[cfg.name])["BranchName"] == cfg.branch

    # And they are genuinely different, which is the point of two apps.
    assert _branch(webs["staging"])["BranchName"] != _branch(webs["prod"])["BranchName"]


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_branch_builds_on_push(webs, env_name):
    assert _branch(webs[env_name])["EnableAutoBuild"] is True


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_build_uses_the_lockfile(webs, env_name):
    """npm ci, not npm install. A build that quietly resolves a different
    dependency tree than the one the tests ran against is worth ruling out."""
    build_spec = _app(webs[env_name])["BuildSpec"]
    assert "npm ci" in build_spec
    assert "npm install" not in build_spec
    assert "baseDirectory: dist" in build_spec


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_build_uses_the_same_node_version_as_ci(webs, env_name):
    """Amplify otherwise uses whatever its build image defaults to, and a green
    CI run then says nothing about the build that actually deploys - which is the
    only reason the CI job exists."""
    build_spec = _app(webs[env_name])["BuildSpec"]
    assert ".nvmrc" in build_spec, "the build does not pin a Node version"


# --- the values the bundle is built with ---

@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_every_backend_value_is_a_reference_not_a_literal(webs, env_name):
    """A copied value that drifts rejects every authenticated request with an
    error that reads like a broken login rather than a typo."""
    env = _env(webs[env_name])
    for name in (
        "VITE_API_BASE_URL",
        "VITE_COGNITO_USER_POOL_ID",
        "VITE_COGNITO_USER_POOL_CLIENT_ID",
        "VITE_COGNITO_DOMAIN",
    ):
        assert isinstance(env[name], dict), f"{name} is hardcoded: {env[name]!r}"


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_api_base_url_carries_the_version_prefix(webs, env_name):
    """The API modules use paths relative to it, so without /api/v1 every
    request 404s."""
    rendered = json.dumps(_env(webs[env_name])["VITE_API_BASE_URL"])
    assert "/api/v1" in rendered


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_cognito_domain_has_no_scheme(webs, env_name):
    """Amplify's auth library prepends https://. Including it produces a
    malformed redirect that fails silently - no error, just a sign-in that
    never completes."""
    rendered = json.dumps(_env(webs[env_name])["VITE_COGNITO_DOMAIN"])
    assert "https://" not in rendered
    assert ".auth." in rendered
    assert "amazoncognito.com" in rendered


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_callback_url_points_at_this_branch(webs, env_name):
    """It must also be registered on the Cognito app client, which is a console
    step. If the two disagree, sign-in fails at the redirect with an error page
    from Cognito rather than from this application."""
    rendered = json.dumps(_env(webs[env_name])["VITE_COGNITO_REDIRECT_SIGN_IN"])
    assert "/auth/callback" in rendered
    assert "DefaultDomain" in rendered, "the callback should use the app's own domain"


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_the_frontend_never_runs_in_bypass_mode(webs, env_name):
    """VITE_STRIPE_BYPASS must match the backend's STRIPE_BYPASS. The two select
    code paths that are not equivalent, so a mismatch is a checkout that half
    works."""
    assert _env(webs[env_name])["VITE_STRIPE_BYPASS"] == "false"


@pytest.mark.parametrize("env_name", [cfg.name for cfg in ENVIRONMENTS])
def test_no_secret_key_is_anywhere_near_the_frontend(webs, env_name):
    """The publishable key belongs here and ships in the bundle by design. The
    secret key must never appear in a stack that builds browser JavaScript."""
    rendered = json.dumps(webs[env_name].to_json())
    for marker in ("sk_live_", "sk_test_", "whsec_"):
        assert marker not in rendered, marker
