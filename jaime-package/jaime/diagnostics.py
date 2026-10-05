"""Diagnostics plan generation, validation, and persistence."""

import datetime
import json
import os

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
