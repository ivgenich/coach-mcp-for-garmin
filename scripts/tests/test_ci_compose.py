"""Tests for the Docker Compose path test: scripts/ci-compose.sh, its test-only override
scripts/tests/compose-edge.yaml and edge scripts/tests/compose-edge.Caddyfile, and its CI job.

Standard library only; no Docker. scripts/ci-compose.sh itself runs the stack (CI's
"docker path (compose)" job); these tests keep its files to the rules that make it a fair
stand-in for the Cloudflare tunnel and keep it from touching a real server or leaking a secret.
"""

import os
import re
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SCRIPT = os.path.join(ROOT, "scripts", "ci-compose.sh")
OVERRIDE = os.path.join(HERE, "compose-edge.yaml")
EDGE = os.path.join(HERE, "compose-edge.Caddyfile")
COMPOSE = os.path.join(ROOT, "compose.yaml")
DOCKERFILE = os.path.join(ROOT, "Dockerfile")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "ci.yml")
SECRETS = ("OWNER_SECRET", "MCP_BEARER_TOKEN", "GARMIN_PERSONAL_API_KEY")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def code_lines(text):
    """The lines of a YAML or shell file without comments and blank lines."""
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("#") or not line.strip():
            continue
        out.append(line)
    return out


def blocks(text, indent):
    """{key: its lines} for the keys at exactly `indent` spaces, each with the more indented lines under it."""
    found, key = {}, None
    for line in code_lines(text):
        depth = len(line) - len(line.lstrip(" "))
        if depth == indent and line.rstrip().endswith(":"):
            key = line.strip()[:-1]
            found[key] = []
        elif depth <= indent:
            key = None
        elif key is not None:
            found[key].append(line)
    return found


def section(text, name):
    """The lines under a top-level key (services:, networks:)."""
    return "\n".join(blocks(text, 0).get(name, []))


class Override(unittest.TestCase):
    def setUp(self):
        self.text = read(OVERRIDE)
        self.services = blocks(section(self.text, "services"), 2)

    def test_only_the_edge_publishes_a_port_and_only_on_loopback(self):
        self.assertEqual(set(self.services), {"app", "tunnel", "edge"})
        for name in ("app", "tunnel"):
            with self.subTest(service=name):
                self.assertNotIn("ports:", "\n".join(self.services[name]))
        edge = "\n".join(self.services["edge"])
        ports = re.findall(r"^\s+- (.+)$", edge.split("ports:", 1)[1].split("networks:", 1)[0], re.M)
        self.assertEqual(ports, ["127.0.0.1:${CI_EDGE_PORT:?set by scripts/ci-compose.sh}:8080"])

    def test_the_app_joins_only_the_internal_network(self):
        self.assertIn("    networks: [inside]", self.services["app"])
        networks = blocks(section(self.text, "networks"), 2)
        self.assertEqual(networks["inside"], ["    internal: true"])
        self.assertIn("    networks: [inside, outside]", self.services["edge"])

    def test_the_settings_files_are_the_tests_own(self):
        # !override: compose.yaml's env_file (.env, tunnel.env in the repository) is replaced, not added to.
        for name, var in (("app", "CI_COMPOSE_ENV"), ("tunnel", "CI_COMPOSE_TUNNEL_ENV")):
            with self.subTest(service=name):
                body = "\n".join(self.services[name])
                self.assertIn("env_file: !override", body)
                self.assertIn("${" + var + ":?", body)
        self.assertIn("    profiles: [real-tunnel]", self.services["tunnel"])

    def test_the_edge_waits_for_a_healthy_app_like_the_tunnel(self):
        edge = "\n".join(self.services["edge"])
        self.assertRegex(edge, r"depends_on:\n\s+app:\n\s+condition: service_healthy")
        self.assertIn("no-new-privileges:true", edge)

    def test_compose_yaml_itself_still_publishes_no_port(self):
        self.assertNotIn("ports:", "\n".join(code_lines(read(COMPOSE))))


class Edge(unittest.TestCase):
    def test_the_edge_names_the_caller_like_cloudflare(self):
        lines = [line.strip() for line in code_lines(read(EDGE))]
        self.assertIn("reverse_proxy app:8080 {", lines)  # the tunnel's route (docs/deploy-docker.md)
        self.assertIn("header_up CF-Connecting-IP {remote_host}", lines)
        # The app's header (compose.yaml) is the one the edge sets.
        self.assertIn("EDGE_CLIENT_IP_HEADER: CF-Connecting-IP", read(COMPOSE))


class Script(unittest.TestCase):
    def setUp(self):
        self.text = read(SCRIPT)

    def test_the_edge_image_is_the_dockerfiles_caddy(self):
        expr = re.search(r"EDGE_IMAGE=\$\(sed -n '([^']+)' Dockerfile\)", self.text).group(1)
        out = subprocess.run(["sed", "-n", expr, DOCKERFILE], capture_output=True, text=True, check=True).stdout
        want = re.findall(r"^FROM (caddy:\S+@sha256:[0-9a-f]{64}) AS caddy$", read(DOCKERFILE), re.M)
        self.assertEqual(len(want), 1)
        self.assertEqual(out.splitlines(), want)

    def test_the_real_compose_yaml_is_used_with_a_project_of_its_own(self):
        self.assertIn('-f compose.yaml -f "$OVERRIDE"', self.text)
        self.assertIn('--env-file "$WORK/compose-vars.env"', self.text)  # never the repository's .env
        self.assertIn('-p "$CP"', self.text)
        self.assertIn("trap cleanup EXIT", self.text)
        self.assertIn("down --volumes --rmi local", self.text)

    def test_no_secret_is_printed(self):
        allowed = (
            re.compile(r'^\s*(OWNER_SECRET|MCP_BEARER_TOKEN|GARMIN_PERSONAL_API_KEY)=("fk!)?\$\(openssl rand -hex \d+\)"?$'),
            re.compile(r'^\s*echo "::add-mask::\$(OWNER_SECRET|MCP_BEARER_TOKEN|GARMIN_PERSONAL_API_KEY)"$'),
            re.compile(r'^\s*printf .*"\$OWNER_SECRET" "\$MCP_BEARER_TOKEN"$'),
            re.compile(r"""^\s*if \[ "\$1" = key \]; then printf .* "\$GARMIN_PERSONAL_API_KEY"; fi$"""),
            re.compile(r'^\s*log_hits\(\) \{ grep -cF -e "\$OWNER_SECRET" -e "\$MCP_BEARER_TOKEN" '
                       r'-e "\$GARMIN_PERSONAL_API_KEY" "\$1" \|\| true; \}$'),
        )
        for line in code_lines(self.text):
            if not any("$" + name in line or "${" + name in line for name in SECRETS):
                continue
            with self.subTest(line=line.strip()):
                self.assertTrue(any(p.match(line) for p in allowed), "a secret value is used here")
        # Both printf lines write into the test's .env (mode 600 by umask), never to the output.
        self.assertRegex(self.text, r'umask 077\n\s+\{\n(.*\n){4}\s+\} >"\$WORK/app\.env"')


class Workflow(unittest.TestCase):
    def setUp(self):
        self.text = read(WORKFLOW)
        self.job = "\n".join(blocks(section(self.text, "jobs"), 2)["compose"])

    def test_the_job_runs_the_script_on_amd64_and_arm64(self):
        self.assertIn("name: docker path (compose)${{ matrix.label }}", self.job)
        self.assertIn("run: scripts/ci-compose.sh", self.job)
        self.assertEqual(re.findall(r"- runner: (\S+)", self.job), ["ubuntu-24.04", "ubuntu-24.04-arm"])
        self.assertIn("timeout-minutes:", self.job)
        self.assertIn("persist-credentials: false", self.job)

    def test_every_action_is_pinned_by_sha_and_nothing_is_published(self):
        for use in re.findall(r"uses: (\S+)", self.job):
            with self.subTest(uses=use):
                self.assertRegex(use, r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")
        for banned in ("docker push", "ghcr.io", "packages: write", "docker/login-action"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, self.text)
        self.assertRegex(self.text, r"(?m)^permissions:\n  contents: read$")


if __name__ == "__main__":
    unittest.main()
