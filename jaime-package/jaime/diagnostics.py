"""Diagnostics plan generation, validation, and persistence."""

import datetime
import json
import os
import re

DIAGNOSTICS_SCHEMA = {
    "type": "object",
    "properties": {
        "principal_name": {"type": "string"},
        "generated_at": {"type": "string"},
        "monitoring_plan": {
            "type": "object",
            "properties": {
                "log_files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                            "description": {"type": "string"},
                        },
                        "required": ["path", "priority", "description"],
                    },
                },
                "processes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "expected_min_count": {"type": "integer"},
                            "expected_max_count": {"type": "integer"},
                            "parent": {"type": "string"},
                        },
                        "required": ["name"],
                    },
                },
                "env_variables": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "network": {
                    "type": "object",
                    "properties": {
                        "ports": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "port": {"type": "integer"},
                                    "protocol": {"type": "string", "enum": ["tcp", "udp"]},
                                },
                                "required": ["port", "protocol"],
                            },
                        },
                    },
                },
                "systemd_units": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "health_commands": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string"},
                            "timeout_seconds": {"type": "integer"},
                        },
                        "required": ["command", "timeout_seconds"],
                    },
                },
            },
            "required": ["log_files", "processes", "env_variables", "network"],
        },
    },
    "required": ["principal_name", "monitoring_plan"],
}


def validate_diagnostics(plan):
    """Validate a diagnostics plan dict against the schema.

    Returns a list of error strings. An empty list means valid.
    """
    errors = []

    if not isinstance(plan, dict):
        return ["diagnostics must be a JSON object"]

    for field in ["principal_name", "monitoring_plan"]:
        if field not in plan:
            errors.append(f"missing required field: '{field}'")

    mp = plan.get("monitoring_plan")
    if not isinstance(mp, dict):
        # Only add a type error when monitoring_plan was present but wrong type;
        # the missing-field error was already added by the loop above.
        if "monitoring_plan" in plan:
            errors.append("'monitoring_plan' must be a JSON object")
        return errors

    if "log_files" in mp:
        if not isinstance(mp["log_files"], list):
            errors.append("'monitoring_plan.log_files' must be a list")
        else:
            for i, lf in enumerate(mp["log_files"]):
                if not isinstance(lf, dict):
                    errors.append(f"monitoring_plan.log_files[{i}] must be an object")
                    continue
                for f in ("path", "priority", "description"):
                    if f not in lf:
                        errors.append(f"monitoring_plan.log_files[{i}] missing '{f}'")
                if lf.get("priority") not in (None, "high", "medium", "low"):
                    errors.append(f"monitoring_plan.log_files[{i}] 'priority' must be 'high', 'medium', or 'low'")

    if "processes" in mp:
        if not isinstance(mp["processes"], list):
            errors.append("'monitoring_plan.processes' must be a list")
        else:
            for i, proc in enumerate(mp["processes"]):
                if not isinstance(proc, dict):
                    errors.append(f"monitoring_plan.processes[{i}] must be an object")
                    continue
                if "name" not in proc:
                    errors.append(f"monitoring_plan.processes[{i}] missing 'name'")

    if "env_variables" in mp:
        if not isinstance(mp["env_variables"], list):
            errors.append("'monitoring_plan.env_variables' must be a list")
        else:
            for i, ev in enumerate(mp["env_variables"]):
                if not isinstance(ev, str):
                    errors.append(f"monitoring_plan.env_variables[{i}] must be a string")

    if "network" in mp:
        net = mp["network"]
        if not isinstance(net, dict):
            errors.append("'monitoring_plan.network' must be an object")
        elif "ports" in net:
            if not isinstance(net["ports"], list):
                errors.append("'monitoring_plan.network.ports' must be a list")
            else:
                for i, p in enumerate(net["ports"]):
                    if not isinstance(p, dict):
                        errors.append(f"monitoring_plan.network.ports[{i}] must be an object")
                        continue
                    for f in ("port", "protocol"):
                        if f not in p:
                            errors.append(f"monitoring_plan.network.ports[{i}] missing '{f}'")

    if "systemd_units" in mp:
        if not isinstance(mp["systemd_units"], list):
            errors.append("'monitoring_plan.systemd_units' must be a list")
        else:
            for i, u in enumerate(mp["systemd_units"]):
                if not isinstance(u, str):
                    errors.append(f"monitoring_plan.systemd_units[{i}] must be a string")

    if "health_commands" in mp:
        if not isinstance(mp["health_commands"], list):
            errors.append("'monitoring_plan.health_commands' must be a list")
        else:
            for i, cmd in enumerate(mp["health_commands"]):
                if not isinstance(cmd, dict):
                    errors.append(f"monitoring_plan.health_commands[{i}] must be an object")
                    continue
                if "command" not in cmd:
                    errors.append(f"monitoring_plan.health_commands[{i}] missing 'command'")

    return errors


# Item-count caps for the Kubernetes plan (TASKS 4.6). Validation enforces
# them; the collector does not truncate, so an over-cap plan is rejected
# rather than silently collected in part.
K8S_DIAGNOSTICS_CAPS = {
    "applications": 50,
    "containers": 10,
    "log_patterns": 10,
    "env_variables": 50,
    "ports": 20,
}
K8S_MAX_LOG_PATTERN_CHARS = 200
_K8S_PLAN_KEYS = ("containers", "log_patterns", "env_variables", "ports")


def validate_k8s_diagnostics(plan):
    """Validate a Kubernetes diagnostics plan against the k8s schema.

    Unlike the machine plan this is not a file: it is a JSON object keyed by
    application name, each value a k8s-scoped plan. Only what the Kubernetes
    API can observe without exec is allowed (container selection, log
    patterns, env variable names, declared ports).

    Returns a list of error strings. An empty list means valid.
    """
    if not isinstance(plan, dict):
        return ["diagnostics must be a JSON object"]

    errors = []
    if len(plan) > K8S_DIAGNOSTICS_CAPS["applications"]:
        errors.append(
            f"too many applications: {len(plan)} "
            f"(max {K8S_DIAGNOSTICS_CAPS['applications']})"
        )

    for app, cfg in plan.items():
        if not isinstance(cfg, dict):
            errors.append(f"'{app}' must be a JSON object")
            continue

        unknown = [k for k in cfg if k not in _K8S_PLAN_KEYS]
        if unknown:
            errors.append(
                f"'{app}' has unknown keys: {', '.join(sorted(unknown))}"
            )

        _validate_string_list(
            cfg, app, "containers", K8S_DIAGNOSTICS_CAPS["containers"], errors
        )
        _validate_string_list(
            cfg, app, "env_variables", K8S_DIAGNOSTICS_CAPS["env_variables"], errors
        )
        _validate_log_patterns(cfg, app, errors)
        _validate_k8s_ports(cfg, app, errors)

    return errors


def _validate_string_list(cfg, app, key, cap, errors):
    """Validate an optional list-of-non-empty-strings field and its cap."""
    if key not in cfg:
        return
    value = cfg[key]
    if not isinstance(value, list):
        errors.append(f"'{app}.{key}' must be a list")
        return
    if len(value) > cap:
        errors.append(f"'{app}.{key}' has {len(value)} items (max {cap})")
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item:
            errors.append(f"'{app}.{key}[{i}]' must be a non-empty string")


def _validate_log_patterns(cfg, app, errors):
    """Validate log_patterns: strings, bounded length, compilable regexes."""
    if "log_patterns" not in cfg:
        return
    patterns = cfg["log_patterns"]
    if not isinstance(patterns, list):
        errors.append(f"'{app}.log_patterns' must be a list")
        return
    cap = K8S_DIAGNOSTICS_CAPS["log_patterns"]
    if len(patterns) > cap:
        errors.append(f"'{app}.log_patterns' has {len(patterns)} items (max {cap})")
    for i, pattern in enumerate(patterns):
        if not isinstance(pattern, str):
            errors.append(f"'{app}.log_patterns[{i}]' must be a string")
            continue
        if len(pattern) > K8S_MAX_LOG_PATTERN_CHARS:
            errors.append(
                f"'{app}.log_patterns[{i}]' exceeds "
                f"{K8S_MAX_LOG_PATTERN_CHARS} characters"
            )
            continue
        try:
            re.compile(pattern)
        except re.error as e:
            errors.append(f"'{app}.log_patterns[{i}]' is not a valid regex: {e}")


def _validate_k8s_ports(cfg, app, errors):
    """Validate the ports list: {port: int, protocol: tcp|udp}."""
    if "ports" not in cfg:
        return
    ports = cfg["ports"]
    if not isinstance(ports, list):
        errors.append(f"'{app}.ports' must be a list")
        return
    cap = K8S_DIAGNOSTICS_CAPS["ports"]
    if len(ports) > cap:
        errors.append(f"'{app}.ports' has {len(ports)} items (max {cap})")
    for i, port in enumerate(ports):
        if not isinstance(port, dict):
            errors.append(f"'{app}.ports[{i}]' must be an object")
            continue
        number = port.get("port")
        if isinstance(number, bool) or not isinstance(number, int):
            errors.append(f"'{app}.ports[{i}].port' must be an integer")
        protocol = port.get("protocol", "tcp")
        if protocol not in ("tcp", "udp"):
            errors.append(f"'{app}.ports[{i}].protocol' must be 'tcp' or 'udp'")


def build_prompt(principal_name):
    """Build the prompt sent to the AI provider."""
    schema_json = json.dumps(DIAGNOSTICS_SCHEMA, indent=2)

    prompt = (
        "You are a diagnostic planning assistant for Juju charms running on Ubuntu LTS.\n"
        f"\n"
        f"The principal charm name is: {principal_name}\n"
        f"\n"
        f"Generate a monitoring plan for this charm's workload following this JSON schema:\n"
        f"\n"
        f"{schema_json}\n"
        f"\n"
        "Include:\n"
        "- Log files the charm or its workload typically writes (up to 5 items)\n"
        "- Systemd units the workload depends on\n"
        "- Processes the workload runs (include expected count ranges where known)\n"
        "- Environment variables the workload uses for configuration\n"
        "- Network ports the workload listens on\n"
        "- Health commands that can safely check the workload status\n"
        "\n"
        "Use the actual current UTC date and time for the 'generated_at' field.\n"
        "\n"
        "Respond with ONLY valid JSON matching the schema. "
        "Do not include markdown fences, explanations, or extra text. "
        "The response must be parseable as raw JSON."
    )
    return prompt


def write_diagnostics_file(plan, path="/var/lib/jaime/diagnostics.json"):
    """Persist a diagnostics plan to a JSON file.

    Accepts a dict or a JSON string. Returns the file path written.
    """
    if isinstance(plan, str):
        plan_obj = json.loads(plan)
    else:
        plan_obj = plan

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(plan_obj, f, indent=2)

    return path


def read_diagnostics_file(path="/var/lib/jaime/diagnostics.json"):
    """Read a diagnostics plan from a JSON file. Returns None if missing."""
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def make_empty_plan(principal_name):
    """Create a minimal empty diagnostics plan with the required fields."""
    return {
        "principal_name": principal_name,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "monitoring_plan": {
            "log_files": [],
            "processes": [],
            "env_variables": [],
            "network": {"ports": []},
            "systemd_units": [],
            "health_commands": [],
        },
    }
