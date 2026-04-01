import os

from deploy_manager.config.settings import (
    DEFAULT_NODE_BIN,
    DEFAULT_NPM_BIN,
    DJANGO_DEFAULT_WSGI,
    SERVICE_DIR,
    SYSTEMD_DIR,
)
from deploy_manager.core.exceptions import DeployError
from deploy_manager.core.utils import confirm, run_cmd
from deploy_manager.operations.users import ensure_system_user
from deploy_manager.projects.helpers import (
    get_dest_dir,
    get_venv_bin,
    is_python_type,
    needs_service,
)


def _build_unit_lines(description, user, working_dir, exec_start, env_file=None, extra_env=None):
    home_dir = f"/var/lib/{user}"
    lines = [
        "[Unit]", f"Description={description}", "After=network.target", "",
        "[Service]", "Type=simple", f"User={user}", f"Group={user}",
        f"WorkingDirectory={working_dir}", f"ExecStart={exec_start}",
        f"Environment=HOME={home_dir}",
    ]
    for k, v in (extra_env or {}).items():
        lines.append(f"Environment={k}={v}")
    if env_file:
        lines.append(f"EnvironmentFile={env_file}")
    rw_paths = f"{working_dir} {home_dir}"
    lines.extend([
        "Restart=on-failure", "RestartSec=5", "StartLimitIntervalSec=60", "StartLimitBurst=5",
        "StandardOutput=journal", "StandardError=journal",
        f"SyslogIdentifier={description}",
        "", "# Hardening", "NoNewPrivileges=true", "ProtectSystem=strict",
        f"ReadWritePaths={rw_paths}", "ProtectHome=true", "PrivateTmp=true",
        "", "[Install]", "WantedBy=multi-user.target",
    ])
    return "\n".join(lines) + "\n"


def _build_compose_unit(description, user, working_dir, exec_start, exec_stop, env_file=None):
    lines = [
        "[Unit]", f"Description={description}",
        "After=network.target docker.service",
        "Requires=docker.service",
        "",
        "[Service]", "Type=oneshot", "RemainAfterExit=yes",
        f"User={user}", f"Group={user}",
        f"WorkingDirectory={working_dir}",
        f"ExecStart=/bin/bash -c '{exec_start}'",
        f"ExecStop=/bin/bash -c '{exec_stop}'",
    ]
    if env_file:
        lines.append(f"EnvironmentFile={env_file}")
    lines.extend([
        "StandardOutput=journal", "StandardError=journal",
        f"SyslogIdentifier={description}",
        "", "[Install]", "WantedBy=multi-user.target",
    ])
    return "\n".join(lines) + "\n"


def generate_service_unit(proj, port, entry_point, workers=2, npm_script=None, env_file=None):
    dest_dir = get_dest_dir(proj)
    ptype = proj["type"]
    desc = proj["name"]
    user = proj["user"]
    home_dir = f"/var/lib/{user}"
    extra_env = {}

    if ptype == "fastapi":
        exec_start = f"{get_venv_bin(proj, 'uvicorn')} {entry_point} --host 0.0.0.0 --port {port} --workers {workers}"

    elif ptype == "django":
        wsgi_module = entry_point
        exec_start = f"{get_venv_bin(proj, 'gunicorn')} {wsgi_module} --bind 0.0.0.0:{port} --workers {workers}"
        settings = proj.get("django_settings", "")
        if settings:
            extra_env["DJANGO_SETTINGS_MODULE"] = settings

    elif ptype == "nodeapi":
        pkg = proj.get("pkg_cmd", DEFAULT_NPM_BIN)
        if npm_script:
            exec_start = f"{pkg} run {npm_script}"
        else:
            exec_start = f"{DEFAULT_NODE_BIN} {entry_point}"
        extra_env["NODE_ENV"] = "production"
        extra_env["PORT"] = str(port)
        extra_env["NPM_CONFIG_CACHE"] = os.path.join(home_dir, ".npm")

    elif ptype == "nextapp":
        pkg = proj.get("pkg_cmd", DEFAULT_NPM_BIN)
        if npm_script:
            exec_start = f"{pkg} run {npm_script}"
        else:
            exec_start = f"{pkg} start"
        extra_env["NODE_ENV"] = "production"
        extra_env["PORT"] = str(port)
        extra_env["NPM_CONFIG_CACHE"] = os.path.join(home_dir, ".npm")

    elif ptype == "compose":
        app_dir = os.path.join(dest_dir, proj["app_dir"]) if proj.get("app_dir") else dest_dir
        compose_file = proj.get("compose_file", "docker-compose.yml")
        exec_start = f"docker compose -f {compose_file} up -d --remove-orphans"
        exec_stop  = f"docker compose -f {compose_file} down"
        return _build_compose_unit(desc, user, app_dir, exec_start, exec_stop, env_file)

    else:
        raise DeployError(f"Cannot generate service for type: {ptype}")

    return _build_unit_lines(desc, user, dest_dir, exec_start, env_file, extra_env)


def link_service_file(proj):
    """Symlink a .service file that lives inside /srv/<type>/<name>/ into SYSTEMD_DIR."""
    service_name = proj.get("service", "")
    if not service_name:
        raise DeployError("No 'service' name configured for this project.")

    src_path = os.path.join(SERVICE_DIR, service_name)

    if not os.path.isfile(src_path):
        raise DeployError(
            f"Service file not found at {src_path}\n"
            f"  Generate it first with option 14, or place it manually in {SERVICE_DIR}."
        )

    link_path = os.path.join(SYSTEMD_DIR, service_name)

    if os.path.islink(link_path):
        current_target = os.readlink(link_path)
        if current_target == src_path:
            print(f"  Already linked: {link_path} -> {src_path}")
            return
        print(f"  Removing existing symlink: {link_path} -> {current_target}")
        os.remove(link_path)
    elif os.path.isfile(link_path):
        if not confirm(f"  {link_path} is a regular file (not a symlink). Replace with symlink?"):
            return
        os.remove(link_path)

    os.symlink(src_path, link_path)
    print(f"  Linked: {link_path} -> {src_path}")
    run_cmd(["systemctl", "daemon-reload"])
    run_cmd(["systemctl", "enable", service_name])
    print(f"  Service {service_name} enabled.")


def create_service_file(proj, interactive=True):
    if not needs_service(proj):
        return
    service_name = proj.get("service", "")
    if not service_name:
        raise DeployError("No 'service' name configured.")

    dest_dir = get_dest_dir(proj)
    ptype = proj["type"]
    port = proj.get("port", 3000)
    entry_point = proj.get("entry_point", "") or proj.get("wsgi_module", "")
    env_file_path = os.path.join(dest_dir, proj.get("env_file", ".env"))
    env_file = env_file_path if os.path.isfile(env_file_path) else None
    workers = 2
    npm_script = proj.get("npm_script")

    if interactive:
        print(f"\n  Creating systemd service: {service_name}")
        print(f"  Type: {ptype} | User: {proj['user']} | Dir: {dest_dir}")

    if ptype == "compose":
        compose_file = proj.get("compose_file", "docker-compose.yml")
        if interactive:
            cf_input = input(f"  Compose file [{compose_file}]: ").strip()
            if cf_input:
                compose_file = cf_input
    else:
        if interactive:
            port_input = input(f"  Port [{port}]: ").strip()
            if port_input.isdigit():
                port = int(port_input)

        if ptype == "fastapi":
            default_ep = entry_point or "app.main:app"
            if interactive:
                ep_input = input(f"  Uvicorn ASGI app [{default_ep}]: ").strip()
                entry_point = ep_input or default_ep
            else:
                entry_point = entry_point or default_ep
        elif ptype == "django":
            default_ep = entry_point or proj.get("wsgi_module", DJANGO_DEFAULT_WSGI)
            if interactive:
                ep_input = input(f"  Gunicorn WSGI module [{default_ep}]: ").strip()
                entry_point = ep_input or default_ep
            else:
                entry_point = entry_point or default_ep
        elif ptype in ("nodeapi", "nextapp"):
            default_ep = entry_point or "src/index.js"
            if interactive:
                ep_input = input(f"  Entry point [{default_ep}]: ").strip()
                entry_point = ep_input or default_ep
                npm_input = input(f"  Use npm script instead? (e.g. 'start') [leave empty]: ").strip()
                if npm_input:
                    npm_script = npm_input
            else:
                entry_point = entry_point or default_ep

        if is_python_type(proj) and interactive:
            w_input = input(f"  Workers [{workers}]: ").strip()
            workers = int(w_input) if w_input.isdigit() else workers

    unit_content = generate_service_unit(proj, port, entry_point, workers, npm_script, env_file)

    # Write to /srv/service/<name>.service (flat, no subfolders)
    os.makedirs(SERVICE_DIR, exist_ok=True)
    srv_service_path = os.path.join(SERVICE_DIR, service_name)
    if os.path.isfile(srv_service_path) and interactive:
        if not confirm(f"  {srv_service_path} exists. Overwrite?"):
            return
    with open(srv_service_path, "w") as f:
        f.write(unit_content)
    print(f"  Written: {srv_service_path}")

    ensure_system_user(proj["user"])
    link_service_file(proj)
