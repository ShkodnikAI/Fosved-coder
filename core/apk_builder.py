"""
Fosved Coder — APK Builder Module
Сборка .apk в зависимости от шаблона проекта.
Поддержка: React, Next.js, Expo, FastAPI, Flask, Python CLI.
Автоматическая генерация иконки через AI.
"""
import os
import json
import shutil
import asyncio
import subprocess
from datetime import datetime
from pathlib import Path

from core.action_logger import get_logger
logger = get_logger()


class APKBuildConfig:
    """Настройки сборки APK для проекта. Сохраняются в JSON."""

    DEFAULTS = {
        "app_name": "",
        "package_id": "",          # com.company.appname
        "app_version": "1.0.0",
        "app_version_code": 1,
        "app_icon_prompt": "",     # custom prompt for icon generation (optional)
        "app_description": "",
        "app_color": "#8B1A1A",   # primary color for splash screen / icon hint
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
            "description": "WebView обёртка для FastAPI backend",
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

    def _substitute(self, cmd: str) -> str:
        """Replace {variables} in command strings."""
        cmd = cmd.replace("{app_name}", self.data["app_name"] or "MyApp")
        cmd = cmd.replace("{package_id}", self.data["package_id"] or "com.example.app")
        cmd = cmd.replace("{build_type}", self.data["build_type"])
        return cmd

    def get_build_commands(self, template: str) -> list[str]:
        """Get final build commands with variables substituted."""
        strategy = self.get_strategy(template)
        if not strategy:
            return []
        return [self._substitute(cmd) for cmd in strategy["build_commands"]]

    def get_init_commands(self, template: str) -> list[str]:
        """Get init/setup commands with variables substituted."""
        strategy = self.get_strategy(template)
        if not strategy:
            return []
        return [self._substitute(cmd) for cmd in strategy["init_commands"]]

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

        if strategy["engine"] in ("capacitor", "capacitor-webview"):
            # Search in all common APK output directories
            search_dirs = [
                os.path.join(project_path, "android", "app", "build", "outputs", "apk", build_type),
                os.path.join(project_path, "android", "app", "build", "outputs", "apk"),
            ]
            for search_dir in search_dirs:
                if os.path.exists(search_dir):
                    for f in sorted(os.listdir(search_dir)):
                        if f.endswith(".apk"):
                            return os.path.join(search_dir, f)

        elif strategy["engine"] == "buildozer":
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

    # ─────────────────────────────────────────────
    #  ICON GENERATION (AI-powered via z-ai-generate)
    # ─────────────────────────────────────────────

    async def generate_icon(self, project_path: str, config: APKBuildConfig,
                            app_description: str = "") -> dict:
        """
        Generate app icon using AI (z-ai-generate CLI).
        Places the icon in the correct location for the build engine.
        Returns dict with: success, icon_path, prompt_used.
        """
        app_name = config.data.get("app_name", "App")
        app_color = config.data.get("app_color", "#8B1A1A")
        custom_prompt = config.data.get("app_icon_prompt", "")

        # Build prompt for icon generation
        if custom_prompt:
            prompt = f"App icon for {app_name}: {custom_prompt}"
        else:
            desc_part = f", theme: {app_description}" if app_description else ""
            prompt = (
                f"Professional mobile app icon for '{app_name}'{desc_part}. "
                f"Modern flat design, clean minimalist style, "
                f"primary color {app_color}, no text, rounded square, "
                f"high contrast, suitable for Android launcher"
            )

        # Output path — store in project's android resources
        icon_dir = os.path.join(project_path, "android", "app", "src", "main", "res")
        icon_path = os.path.join(project_path, "android", "app", "src", "main", "res",
                                  "mipmap-xxxhdpi", "ic_launcher.png")

        # Ensure directory exists
        os.makedirs(os.path.dirname(icon_path), exist_ok=True)

        print(f"  [apk] Генерация иконки: {prompt[:80]}...")

        # Call z-ai-generate CLI
        try:
            result = await asyncio.create_subprocess_shell(
                f'z-ai-generate -p "{prompt}" -o "{icon_path}" -s 1024x1024',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(result.communicate(), timeout=120)

            if result.returncode == 0 and os.path.exists(icon_path) and os.path.getsize(icon_path) > 1000:
                print(f"  [apk] Иконка сгенерирована: {icon_path} ({os.path.getsize(icon_path)} bytes)")

                # Copy to other mipmap directories for completeness
                mipmap_sizes = {
                    "mipmap-mdpi": 48, "mipmap-hdpi": 72, "mipmap-xhdpi": 96,
                    "mipmap-xxhdpi": 144, "mipmap-xxxhdpi": 192,
                }
                for folder, _ in mipmap_sizes.items():
                    target = os.path.join(icon_dir, folder)
                    os.makedirs(target, exist_ok=True)
                    target_file = os.path.join(target, "ic_launcher.png")
                    if not os.path.exists(target_file):
                        shutil.copy2(icon_path, target_file)

                # Also set as adaptive icon foreground
                adaptive_dir = os.path.join(icon_dir, "mipmap-xxxhdpi")
                adaptive_path = os.path.join(adaptive_dir, "ic_launcher_foreground.png")
                if not os.path.exists(adaptive_path):
                    shutil.copy2(icon_path, adaptive_path)

                return {
                    "success": True,
                    "icon_path": icon_path,
                    "prompt_used": prompt,
                    "icon_size": os.path.getsize(icon_path),
                }
            else:
                err_msg = stderr.decode("utf-8", errors="replace")[:200]
                print(f"  [apk] Не удалось сгенерировать иконку: {err_msg}")
                return {
                    "success": False,
                    "error": f"z-ai-generate не удался: {err_msg}",
                    "prompt_used": prompt,
                }
        except asyncio.TimeoutError:
            try:
                logger.log("icon_generation_timeout", level="warning", source="apk_builder",
                           error="Icon generation timed out (120s)")
            except Exception:
                pass
            return {"success": False, "error": "Таймаут генерации иконки (120s)", "prompt_used": prompt}
        except Exception as e:
            try:
                logger.log("icon_generation_error", level="error", source="apk_builder", error=str(e))
            except Exception:
                pass
            return {"success": False, "error": str(e), "prompt_used": prompt}

    # ─────────────────────────────────────────────
    #  PLATFORM INIT
    # ─────────────────────────────────────────────

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
                "step": i, "total": len(init_commands), "cmd": cmd,
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

    # ─────────────────────────────────────────────
    #  BUILD
    # ─────────────────────────────────────────────

    async def build(self, project_path: str, template: str, config: APKBuildConfig,
                    app_description: str = "") -> dict:
        """Build APK with auto-generated icon. Returns result with path to APK."""
        strategy = config.get_strategy(template)
        if not strategy:
            return {"success": False, "error": f"Шаблон '{template}' не поддерживается"}

        # Validate
        errors = config.validate(template)
        if errors:
            return {"success": False, "error": "; ".join(errors)}

        # Check project path
        if not os.path.isdir(project_path):
            return {"success": False, "error": f"Путь проекта не найден: {project_path}"}

        try:
            logger.log("apk_build_start", level="info", source="apk_builder",
                       details={"template": template, "project_path": project_path,
                                "strategy": strategy["name"], "build_type": config.data["build_type"]})
        except Exception:
            pass

        build_log = []

        # ── Step 1: Generate icon ──
        print(f"  [apk] Шаг: генерация иконки...")
        icon_result = await self.generate_icon(project_path, config, app_description)
        build_log.append({
            "step": 0, "cmd": "AI icon generation",
            "success": icon_result["success"],
            "output": icon_result.get("prompt_used", "") if icon_result["success"]
                       else icon_result.get("error", ""),
        })
        if icon_result["success"]:
            print(f"  [apk] Иконка готова: {icon_result['icon_path']}")
        else:
            print(f"  [apk] Иконка не сгенерирована (не критично): {icon_result.get('error', '')}")

        # ── Step 2: Git checkpoint ──
        if config.data.get("auto_git_commit"):
            git_result = await self._exec(
                'git add -A && git commit -m "[auto] before APK build" --allow-empty',
                project_path
            )
            if git_result["success"]:
                print(f"  [apk] Git checkpoint создан")

        # ── Step 3: Pre-build ──
        pre_build = strategy.get("pre_build", [])
        for cmd in pre_build:
            result = await self._exec(cmd, project_path)
            if not result["success"]:
                return {
                    "success": False,
                    "error": f"Pre-build не удался: {cmd}",
                    "output": result.get("stderr", ""),
                    "build_log": build_log,
                }

        # ── Step 4: Build commands ──
        build_commands = config.get_build_commands(template)
        for i, cmd in enumerate(build_commands, 1):
            print(f"  [apk] Build step {i}/{len(build_commands)}: {cmd}")
            result = await self._exec(cmd, project_path)
            build_log.append({
                "step": i, "cmd": cmd,
                "success": result["success"],
                "output": result.get("stdout", "") + result.get("stderr", ""),
            })
            if not result["success"]:
                try:
                    logger.log(f"apk_build_step_failed", level="error", source="apk_builder",
                               details={"step": i, "cmd": cmd[:200]},
                               error=result.get("stderr", "")[:300])
                except Exception:
                    pass
                return {
                    "success": False,
                    "error": f"Build не удался на шаге {i}: {cmd}",
                    "build_log": build_log,
                }

        # ── Step 5: Find and rename APK ──
        apk_path = config.find_apk_output(project_path, template)
        final_apk_path = apk_path

        # Rename APK to {app_name}.apk (or {app_name}-{version}.apk)
        if apk_path and os.path.exists(apk_path):
            app_name = config.data.get("app_name", "app").lower().replace(" ", "-")
            app_version = config.data.get("app_version", "1.0.0")
            apk_dir = os.path.dirname(apk_path)
            new_name = f"{app_name}-{app_version}.apk"
            final_apk_path = os.path.join(apk_dir, new_name)
            try:
                shutil.move(apk_path, final_apk_path)
                print(f"  [apk] APK переименован: {new_name}")
            except Exception as e:
                print(f"  [apk] Не удалось переименовать APK: {e}")
                final_apk_path = apk_path

        try:
            logger.log("apk_build_success", level="success", source="apk_builder",
                       details={"apk_path": final_apk_path, "template": template,
                                "strategy": strategy["name"], "steps": len(build_commands)})
        except Exception:
            pass

        return {
            "success": True,
            "message": "APK успешно собран!",
            "apk_path": final_apk_path,
            "apk_filename": os.path.basename(final_apk_path) if final_apk_path else None,
            "icon_generated": icon_result["success"],
            "icon_path": icon_result.get("icon_path"),
            "build_log": build_log,
            "build_type": config.data["build_type"],
            "app_name": config.data["app_name"],
            "strategy": strategy["name"],
        }

    # ─────────────────────────────────────────────
    #  ENVIRONMENT CHECK
    # ─────────────────────────────────────────────

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
            "z_ai_generate": {"cmd": "z-ai-generate --help", "label": "z-ai-generate (иконки)", "required_for": []},
        }

        strategy = APKBuildConfig.TEMPLATE_STRATEGIES.get(template)
        required_tools = []
        if strategy:
            required_tools = [c for c in checks if template in checks[c]["required_for"]]

        results = {}
        all_ok = True
        for tool_name, tool_info in checks.items():
            # Always check z-ai-generate (for icon), plus required tools
            is_required = tool_name in required_tools
            if not is_required and tool_name != "z_ai_generate":
                continue
            result = await self._exec(tool_info["cmd"], cwd=None)
            installed = result["success"]
            results[tool_name] = {
                "label": tool_info["label"],
                "installed": installed,
                "version": result.get("stdout", "").strip() if installed else None,
                "required": is_required,
                "optional": tool_name == "z_ai_generate",
            }
            if not installed and is_required:
                all_ok = False

        return {
            "all_ready": all_ok,
            "template": template,
            "strategy": strategy["name"] if strategy else None,
            "tools": results,
        }
