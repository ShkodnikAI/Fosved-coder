"""
Fosved Coder — APK Builder Module
Сборка .apk в зависимости от шаблона проекта.
Поддержка: React, Next.js, Expo, FastAPI, Flask, Python CLI.
"""
import os
import json
import asyncio
from datetime import datetime
from pathlib import Path


class APKBuildConfig:
    """Настройки сборки APK для проекта. Сохраняются в JSON."""

    DEFAULTS = {
        "app_name": "",
        "package_id": "",          # com.company.appname
        "app_version": "1.0.0",
        "app_version_code": 1,
        "app_icon": "",            # path to icon (512x512)
        "app_description": "",
        "app_color": "#8B1A1A",   # primary color for splash screen
        "build_type": "debug",     # "debug" or "release"
        "keystore_path": "",       # for release builds
        "keystore_password": "",
        "keystore_alias": "",
        "android_min_sdk": 24,     # Android 7.0
        "android_target_sdk": 34,  # Android 14
        "auto_git_commit": True,   # commit before build
    }

    # Build strategy per template
    TEMPLATE_STRATEGIES = {
        "react": {
            "engine": "capacitor",
            "name": "Capacitor (React + Vite)",
            "description": "Web-app обёрнутая в нативный Android контейнер через Capacitor",
            "requires": ["node_modules", "package.json"],
            "pre_build": ["npm install", "npm run build"],
            "init_commands": [
                "npm install @capacitor/core @capacitor/cli @capacitor/android",
                "npx cap init \"{app_name}\" \"{package_id}\" --web-dir dist",
                "npx cap add android",
            ],
            "build_commands": [
                "npm run build",
                "npx cap sync",
                "npx cap open android || cd android && ./gradlew assemble{build_type}",
            ],
        },
        "nextjs": {
            "engine": "capacitor",
            "name": "Capacitor (Next.js)",
            "description": "Next.js export → обёртка в Android через Capacitor",
            "requires": ["node_modules", "package.json", "next.config"],
            "pre_build": ["npm install"],
            "init_commands": [
                "npm install @capacitor/core @capacitor/cli @capacitor/android",
                "npx cap init \"{app_name}\" \"{package_id}\" --web-dir out",
                "npx cap add android",
            ],
            "build_commands": [
                "npx next build && npx next export",
                "npx cap sync",
                "cd android && ./gradlew assemble{build_type}",
            ],
        },
        "expo": {
            "engine": "eas",
            "name": "EAS Build (Expo SDK 53)",
            "description": "Нативная сборка через Expo Application Services",
            "requires": ["node_modules", "package.json", "app.json"],
            "pre_build": ["npm install", "npx expo install"],
            "init_commands": [
                "npx expo install expo-dev-client",
            ],
            "build_commands": [
                "eas build --platform android --profile {build_type} --no-wait",
            ],
        },
        "fastapi": {
            "engine": "capacitor-webview",
            "name": "Capacitor WebView (FastAPI)",
            "description": "WebView обёртка для FastAPI backend —需要一个 фронтенд",
            "requires": ["requirements.txt"],
            "pre_build": [],
            "init_commands": [
                "npm init -y",
                "npm install @capacitor/core @capacitor/cli @capacitor/android",
                "npx cap init \"{app_name}\" \"{package_id}\" --web-dir www",
                "mkdir -p www",
                "npx cap add android",
            ],
            "build_commands": [
                "npx cap sync",
                "cd android && ./gradlew assemble{build_type}",
            ],
        },
        "flask": {
            "engine": "capacitor-webview",
            "name": "Capacitor WebView (Flask)",
            "description": "WebView обёртка для Flask backend",
            "requires": ["requirements.txt"],
            "pre_build": [],
            "init_commands": [
                "npm init -y",
                "npm install @capacitor/core @capacitor/cli @capacitor/android",
                "npx cap init \"{app_name}\" \"{package_id}\" --web-dir www",
                "mkdir -p www",
                "npx cap add android",
            ],
            "build_commands": [
                "npx cap sync",
                "cd android && ./gradlew assemble{build_type}",
            ],
        },
        "python-cli": {
            "engine": "buildozer",
            "name": "Buildozer (Kivy)",
            "description": "Нативная Android сборка Python CLI через Buildozer (Kivy)",
            "requires": ["requirements.txt"],
            "pre_build": [],
            "init_commands": [
                "pip install buildozer",
                "buildozer init",
            ],
            "build_commands": [
                "buildozer android {build_type}",
            ],
        },
    }

    def __init__(self, data: dict = None):
        self.data = {**self.DEFAULTS, **(data or {})}

    def to_dict(self) -> dict:
        return dict(self.data)

    @classmethod
    def from_dict(cls, data: dict) -> "APKBuildConfig":
        return cls(data)

    @classmethod
    def from_json(cls, json_str: str) -> "APKBuildConfig":
        return cls(json.loads(json_str) if json_str else {})

    def to_json(self) -> str:
        return json.dumps(self.data, ensure_ascii=False, indent=2)

    def get_strategy(self, template: str) -> dict | None:
        """Get build strategy for a template."""
        return self.TEMPLATE_STRATEGIES.get(template)

    def get_build_commands(self, template: str) -> list[str]:
        """Get final build commands with variables substituted."""
        strategy = self.get_strategy(template)
        if not strategy:
            return []

        commands = []
        for cmd in strategy["build_commands"]:
            cmd = cmd.replace("{app_name}", self.data["app_name"] or "MyApp")
            cmd = cmd.replace("{package_id}", self.data["package_id"] or "com.example.app")
            cmd = cmd.replace("{build_type}", self.data["build_type"])
            commands.append(cmd)
        return commands

    def get_init_commands(self, template: str) -> list[str]:
        """Get init/setup commands with variables substituted."""
        strategy = self.get_strategy(template)
        if not strategy:
            return []

        commands = []
        for cmd in strategy["init_commands"]:
            cmd = cmd.replace("{app_name}", self.data["app_name"] or "MyApp")
            cmd = cmd.replace("{package_id}", self.data["package_id"] or "com.example.app")
            cmd = cmd.replace("{build_type}", self.data["build_type"])
            commands.append(cmd)
        return commands

    def validate(self, template: str) -> list[str]:
        """Validate config. Returns list of error messages."""
        errors = []
        if not self.data["app_name"]:
            errors.append("Укажите название приложения (App Name)")
        if not self.data["package_id"]:
            errors.append("Укажите Package ID (например: com.mycompany.myapp)")
        elif not self._validate_package_id(self.data["package_id"]):
            errors.append("Package ID должен быть в формате com.company.app (только буквы, цифры, точки)")
        if not template or template not in self.TEMPLATE_STRATEGIES:
            errors.append(f"Шаблон '{template}' не поддерживает сборку APK")
        return errors

    @staticmethod
    def _validate_package_id(pid: str) -> bool:
        """Validate Android package identifier format."""
        if not pid or len(pid) < 3:
            return False
        parts = pid.split(".")
        if len(parts) < 2:
            return False
        import re
        return bool(re.match(r'^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$', pid))

    def find_apk_output(self, project_path: str, template: str) -> str | None:
        """Find the built APK file path."""
        strategy = self.get_strategy(template)
        if not strategy:
            return None

        build_type = self.data["build_type"]
        apk_name = f"{build_type.capitalize()}" if build_type == "debug" else "Release"

        if strategy["engine"] in ("capacitor", "capacitor-webview"):
            # Capacitor output
            apk_dir = os.path.join(project_path, "android", "app", "build", "outputs", "apk")
            if os.path.exists(apk_dir):
                for f in os.listdir(apk_dir):
                    if f.endswith(".apk"):
                        return os.path.join(apk_dir, f)
            # Alternative path
            alt_dir = os.path.join(project_path, "android", "app", "build", "outputs", "apk", build_type)
            if os.path.exists(alt_dir):
                for f in os.listdir(alt_dir):
                    if f.endswith(".apk"):
                        return os.path.join(alt_dir, f)

        elif strategy["engine"] == "buildozer":
            # Buildozer output
            bin_dir = os.path.join(project_path, "bin")
            if os.path.exists(bin_dir):
                for f in os.listdir(bin_dir):
                    if f.endswith(".apk"):
                        return os.path.join(bin_dir, f)

        return None


class APKBuilder:
    """Orchestrates the APK build process for a project."""

    def __init__(self, executor=None):
        self.executor = executor

    async def _exec(self, cmd: str, cwd: str) -> dict:
        """Execute a command using the executor or fallback."""
        if self.executor:
            return await self.executor.execute(cmd, cwd=cwd, need_approval=False, timeout=300)
        # Fallback: direct subprocess
        try:
            process = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
            return {
                "exit_code": process.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "success": process.returncode == 0,
                "cmd": cmd,
            }
        except asyncio.TimeoutError:
            return {"exit_code": -1, "stdout": "", "stderr": "Timeout (300s)", "success": False, "cmd": cmd}

    async def init_platform(self, project_path: str, template: str, config: APKBuildConfig) -> dict:
        """Initialize Android platform for the project (first time setup)."""
        strategy = config.get_strategy(template)
        if not strategy:
            return {"success": False, "error": f"Шаблон '{template}' не поддерживается"}

        init_commands = config.get_init_commands(template)

        results = []
        for i, cmd in enumerate(init_commands, 1):
            result = await self._exec(cmd, project_path)
            results.append({
                "step": i,
                "total": len(init_commands),
                "cmd": cmd,
                "success": result["success"],
                "output": result.get("stdout", "") + result.get("stderr", ""),
            })
            if not result["success"]:
                return {
                    "success": False,
                    "error": f"Шаг {i} не удался: {cmd}",
                    "step_results": results,
                    "completed_steps": i - 1,
                }

        return {
            "success": True,
            "message": f"Платформа Android инициализирована ({len(init_commands)} шагов)",
            "step_results": results,
            "completed_steps": len(init_commands),
        }

    async def build(self, project_path: str, template: str, config: APKBuildConfig) -> dict:
        """Build APK. Returns result with path to APK file."""
        strategy = config.get_strategy(template)
        if not strategy:
            return {"success": False, "error": f"Шаблон '{template}' не поддерживается"}

        # Validate
        errors = config.validate(template)
        if errors:
            return {"success": False, "error": "; ".join(errors)}

        # Check project path exists
        if not os.path.isdir(project_path):
            return {"success": False, "error": f"Путь проекта не найден: {project_path}"}

        # Pre-build git commit
        if config.data.get("auto_git_commit"):
            git_result = await self._exec(
                'git add -A && git commit -m "[auto] before APK build" --allow-empty',
                project_path
            )
            if git_result["success"]:
                print(f"  [apk] Git checkpoint создан")

        # Run pre-build commands
        pre_build = strategy.get("pre_build", [])
        for cmd in pre_build:
            result = await self._exec(cmd, project_path)
            if not result["success"]:
                return {
                    "success": False,
                    "error": f"Pre-build не удался: {cmd}",
                    "output": result.get("stderr", ""),
                }

        # Run build commands
        build_commands = config.get_build_commands(template)
        build_log = []
        last_result = None

        for i, cmd in enumerate(build_commands, 1):
            print(f"  [apk] Build step {i}/{len(build_commands)}: {cmd}")
            result = await self._exec(cmd, project_path)
            build_log.append({
                "step": i,
                "cmd": cmd,
                "success": result["success"],
                "output": result.get("stdout", "") + result.get("stderr", ""),
            })
            last_result = result
            if not result["success"]:
                return {
                    "success": False,
                    "error": f"Build не удался на шаге {i}: {cmd}",
                    "build_log": build_log,
                }

        # Find APK file
        apk_path = config.find_apk_output(project_path, template)

        return {
            "success": True,
            "message": "APK успешно собран!",
            "apk_path": apk_path,
            "build_log": build_log,
            "build_type": config.data["build_type"],
            "app_name": config.data["app_name"],
            "strategy": strategy["name"],
        }

    async def check_environment(self, template: str) -> dict:
        """Check if build tools are installed on the system."""
        checks = {
            "node": {"cmd": "node --version", "label": "Node.js", "required_for": ["react", "nextjs", "expo"]},
            "npm": {"cmd": "npm --version", "label": "npm", "required_for": ["react", "nextjs", "expo"]},
            "java": {"cmd": "java -version", "label": "Java JDK", "required_for": ["react", "nextjs", "expo", "fastapi", "flask", "python-cli"]},
            "gradle": {"cmd": "gradle --version", "label": "Gradle", "required_for": ["react", "nextjs", "fastapi", "flask"]},
            "python": {"cmd": "python3 --version", "label": "Python 3", "required_for": ["python-cli", "fastapi", "flask"]},
            "buildozer": {"cmd": "buildozer --version", "label": "Buildozer", "required_for": ["python-cli"]},
            "eas": {"cmd": "eas --version", "label": "EAS CLI", "required_for": ["expo"]},
        }

        strategy = APKBuildConfig.TEMPLATE_STRATEGIES.get(template)
        required_tools = set(strategy["engine"].split("-")[0] if strategy else [])
        if strategy:
            required_tools = [c for c in checks if template in checks[c]["required_for"]]

        results = {}
        all_ok = True
        for tool_name, tool_info in checks.items():
            if tool_name not in required_tools and required_tools:
                continue
            result = await self._exec(tool_info["cmd"], cwd=None)
            installed = result["success"]
            results[tool_name] = {
                "label": tool_info["label"],
                "installed": installed,
                "version": result.get("stdout", "").strip() if installed else None,
                "required": tool_name in (required_tools or []),
            }
            if not installed and tool_info["required"]:
                all_ok = False

        return {
            "all_ready": all_ok,
            "template": template,
            "strategy": strategy["name"] if strategy else None,
            "tools": results,
        }
