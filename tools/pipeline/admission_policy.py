"""The admission policy an extension's wheel is admitted under, declared here.

policy/plugin-admission-v1.json is its committed copy: an admission reads and
hashes that file from the host checkout, and cordon refuses one whose
``plugin_api_version`` is not the version the host speaks, so the number comes
from the host's ``PLUGIN_API_VERSION`` (docs/Plugins.md, Cordon admission).
lint:drift-admission-policy holds the copy to this; its fix mode writes it.
"""

import json

from hq.platform.core.tests.task import usage
from tools.derived import hold
from tools.pipeline.plugin_identity import HOST, plugin_api_version

PATH = HOST / "policy" / "plugin-admission-v1.json"


def policy() -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": "cordon.hq-plugin",
        "host": "severino-hq",
        "plugin_api_version": int(plugin_api_version()),
        "artifact": "python-wheel",
        "required_evidence": [
            "plugin-contract-tests",
            "package-tests",
            "dependency-lock",
            "dependency-audit",
            "secret-scan",
            "wheel-sbom",
        ],
        "admission": {
            "signature": "sigstore-keyless",
            "immutable_source_commit": True,
            "exact_repository": True,
            "exact_workflow_identity": True,
            "exact_artifact_sha256": True,
            "exact_policy_sha256": True,
            "known_vulnerabilities": 0,
        },
    }


def render() -> str:
    return json.dumps(policy(), indent=2) + "\n"


if __name__ == "__main__":
    raise SystemExit(hold(PATH, render().encode(), "lint:drift-admission-policy", fix=usage().switch("fix")))
