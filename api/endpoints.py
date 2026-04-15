import json, os, asyncio, re, shutil, subprocess, sys, platform
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import JSONResponse
from core.keys_manager import KeysManager
from core.chat_history import ChatHistory
from core.chat import stream_chat, get_available_models

router = APIRouter()
keys_mgr = KeysManager()
chat_hist = ChatHistory()
DATA_DIR = "data"
PROJECTS_FILE = os.path.join(DATA_DIR, "projects.json")

def _load_projects():
    if os.path.exists(PROJECTS_FILE):
        with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def _save_projects(projects):
    with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(projects, f, indent=2, ensure_ascii=False)

# ===== KEYS =====
@router.get("/api/keys")
def list_keys():
    return keys_mgr.list_keys()

@router.post("/api/keys")
def add_key(data: dict):
    provider = data.get("provider", "").strip().lower()
    key_value = data.get("key", "").strip()
    name = data.get("name", "").strip()
    models = data.get("models", [])
    if not provider or not key_value:
        return JSONResponse({"error": "provider and key required"}, 400)
    keys_mgr.add_key(provider, key_value, name, models)
    return {"status": "ok"}

@router.delete("/api/keys/{provider}")
def remove_key(provider: str):
    return keys_mgr.remove_key(provider)

# ===== PROVIDERS =====
@router.get("/api/providers")
def list_providers():
    return [
        {"id": "claude", "name": "Claude (Anthropic)"},
        {"id": "openai", "name": "OpenAI"},
        {"id": "openrouter", "name": "OpenRouter"},
        {"id": "grok", "name": "Grok (xAI)"},
        {"id": "google", "name": "Google (Gemini)"},
        {"id": "deepseek", "name": "DeepSeek"},
        {"id": "minimax", "name": "MiniMax"},
        {"id": "custom", "name": "Custom"},
    ]

# ===== MODELS =====
@router.get("/api/models")
def list_models():
    return get_available_models()

# ===== GITHUB =====
@router.get("/api/github/user")
def github_user():
    token = keys_mgr.get_github_token()
    username = keys_mgr.get_github_username()
    return {"token": token, "username": username}

@router.post("/api/github/token")
def set_github_token(data: dict):
    token = data.get("token", "").strip()
    username = data.get("username", "").strip()
    keys_mgr.set_github_token(token, username)
    return {"status": "ok"}

# ===== PROJECTS =====
@router.get("/api/projects")
def list_projects():
    return list(_load_projects().values())

@router.get("/api/projects/{pid}")
def get_project(pid: str):
    projects = _load_projects()
    if pid in projects:
        return projects[pid]
    return JSONResponse({"error": "not found"}, 404)

@router.post("/api/projects")
def create_project(data: dict):
    projects = _load_projects()
    import uuid
    pid = str(uuid.uuid4())[:8]
    name = data.get("name", "New Project").strip()
    folder = data.get("folder", "").strip()
    projects[pid] = {
        "id": pid,
        "name": name,
        "folder": folder,
        "description": data.get("description", ""),
        "prompt": data.get("prompt", ""),
        "instructions": data.get("instructions", ""),
        "ideas": data.get("ideas", ""),
        "github_repo": data.get("github_repo", ""),
        "selected_model": data.get("selected_model", ""),
        "progress": data.get("progress", 0),
        "created": __import__("datetime").datetime.now().isoformat()
    }
    _save_projects(projects)
    # Create project folder if specified
    if folder:
        os.makedirs(folder, exist_ok=True)
    return projects[pid]

@router.put("/api/projects/{pid}")
def update_project(pid: str, data: dict):
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    projects[pid].update(data)
    _save_projects(projects)
    return projects[pid]

@router.delete("/api/projects/{pid}")
def delete_project(pid: str):
    projects = _load_projects()
    if pid in projects:
        del projects[pid]
        _save_projects(projects)
        # Remove chat history
        hist_file = f"data/chat_history/{pid}.json"
        if os.path.exists(hist_file):
            os.remove(hist_file)
        return {"status": "ok"}
    return JSONResponse({"error": "not found"}, 404)

# ===== PROJECT FILES =====
@router.get("/api/projects/{pid}/tree")
def project_tree(pid: str):
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    folder = projects[pid].get("folder", "")
    if not folder or not os.path.isdir(folder):
        return []
    tree = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(('.', '__', 'node_modules', 'venv', '.git'))]
        level = root.replace(folder, "").count(os.sep)
        rel_path = os.path.relpath(root, folder)
        indent = "  " * level
        if level > 0:
            tree.append({"type": "dir", "name": os.path.basename(root), "path": rel_path, "level": level, "indent": indent})
        for f in sorted(files):
            fp = os.path.join(rel_path, f) if rel_path != "." else f
            tree.append({"type": "file", "name": f, "path": fp, "level": level, "indent": indent + "  "})
    return tree

@router.get("/api/projects/{pid}/read-file")
def read_project_file(pid: str, path: str = ""):
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    folder = projects[pid].get("folder", "")
    if not folder:
        return JSONResponse({"error": "no folder set"}, 400)
    full_path = os.path.normpath(os.path.join(folder, path))
    if not full_path.startswith(os.path.normpath(folder)):
        return JSONResponse({"error": "path traversal blocked"}, 403)
    if not os.path.isfile(full_path):
        return JSONResponse({"error": "file not found"}, 404)
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"path": path, "content": content, "lines": content.splitlines()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)

@router.post("/api/projects/{pid}/save-file")
def save_project_file(pid: str, data: dict):
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    folder = projects[pid].get("folder", "")
    if not folder:
        return JSONResponse({"error": "no folder set"}, 400)
    file_path = data.get("path", "")
    content = data.get("content", "")
    full_path = os.path.normpath(os.path.join(folder, file_path))
    if not full_path.startswith(os.path.normpath(folder)):
        return JSONResponse({"error": "path traversal blocked"}, 403)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"status": "ok"}

# ===== THREADS =====
@router.get("/api/projects/{pid}/threads")
def list_threads(pid: str):
    return chat_hist.list_threads(pid)

@router.post("/api/projects/{pid}/threads")
def create_thread(pid: str, data: dict = None):
    if data is None:
        data = {}
    name = data.get("name", "")
    return chat_hist.create_thread(pid, name=name)

@router.put("/api/projects/{pid}/threads/{tid}")
def rename_thread(pid: str, tid: str, data: dict):
    new_name = data.get("name", "")
    return chat_hist.rename_thread(pid, tid, new_name)

@router.delete("/api/projects/{pid}/threads/{tid}")
def delete_thread(pid: str, tid: str):
    return chat_hist.delete_thread(pid, tid)

# ===== CHAT HISTORY =====
@router.get("/api/chat/{pid}")
def get_chat_history(pid: str, thread_id: str = "main"):
    return chat_hist.load_history(pid, thread_id)

@router.delete("/api/chat/{pid}")
def clear_chat_history(pid: str, thread_id: str = "main"):
    return chat_hist.clear_history(pid, thread_id)


# ===== TERMINAL =====
@router.get("/api/projects/{pid}/terminal/cwd")
def terminal_cwd(pid: str):
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    folder = projects[pid].get("folder", "")
    if not folder:
        return JSONResponse({"error": "no folder set"}, 400)
    return {"cwd": folder}

@router.get("/api/projects/{pid}/terminal/history")
def terminal_history(pid: str):
    hist_file = os.path.join(DATA_DIR, f"term_history_{pid}.json")
    if os.path.exists(hist_file):
        with open(hist_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

@router.post("/api/projects/{pid}/terminal/history")
def terminal_save_history(pid: str, data: dict):
    hist_file = os.path.join(DATA_DIR, f"term_history_{pid}.json")
    history = data.get("history", [])
    with open(hist_file, "w", encoding="utf-8") as f:
        json.dump(history[-200:], f)
    return {"status": "ok"}


# ===== REFACTOR =====
# Extensions to analyze as code
CODE_EXTENSIONS = {
    '.py', '.js', '.ts', '.jsx', '.tsx', '.vue', '.html', '.css', '.scss', '.less',
    '.java', '.kt', '.cpp', '.c', '.h', '.hpp', '.cs', '.go', '.rs', '.rb', '.php',
    '.sql', '.sh', '.bash', '.yaml', '.yml', '.json', '.xml', '.toml', '.ini', '.cfg',
    '.env', '.md', '.txt', '.dart', '.swift', '.lua', '.r', '.pl', '.ex', '.exs', '.zig'
}

IGNORE_DIRS = {'.git', '__pycache__', 'node_modules', 'venv', '.venv', '.idea', '.vscode',
               'dist', 'build', '.next', '.nuxt', 'target', '__pypackages__', '.eggs'}

def _collect_project_files(folder, max_files=30, max_size=8000):
    """Collect source code files from project folder for refactoring analysis."""
    files = []
    total_size = 0
    for root, dirs, filenames in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith('.')]
        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in CODE_EXTENSIONS:
                continue
            fpath = os.path.join(root, fname)
            rel_path = os.path.relpath(fpath, folder)
            fsize = os.path.getsize(fpath)
            if fsize > max_size * 3:  # skip huge files
                continue
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(max_size)
                files.append({"path": rel_path, "content": content, "size": fsize, "lang": ext.lstrip('.')})
                total_size += len(content)
            except:
                continue
            if len(files) >= max_files or total_size > max_files * max_size:
                break
        if len(files) >= max_files or total_size > max_files * max_size:
            break
    return files

@router.get("/api/projects/{pid}/refactor/files")
def refactor_collect_files(pid: str, max_files: int = 30):
    """Collect project files for refactoring analysis."""
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    folder = projects[pid].get("folder", "")
    if not folder or not os.path.isdir(folder):
        return JSONResponse({"error": "no folder set"}, 400)
    files = _collect_project_files(folder, max_files)
    return {"files": files, "count": len(files), "project_name": projects[pid].get("name", "")}

@router.post("/api/projects/{pid}/refactor/apply")
def refactor_apply(pid: str, data: dict):
    """Apply a refactoring suggestion to a file."""
    file_path = data.get("path", "")
    content = data.get("content", "")
    if not file_path or content is None:
        return JSONResponse({"error": "path and content required"}, 400)
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    folder = projects[pid].get("folder", "")
    if not folder:
        return JSONResponse({"error": "no folder set"}, 400)
    full_path = os.path.normpath(os.path.join(folder, file_path))
    if not full_path.startswith(os.path.normpath(folder)):
        return JSONResponse({"error": "path traversal blocked"}, 403)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"status": "ok", "path": file_path}


# ===== SEARCH IN FILES =====
@router.get("/api/projects/{pid}/search")
def search_in_files(pid: str, q: str = "", ext: str = ""):
    """Search text across all project files (grep-like)."""
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    folder = projects[pid].get("folder", "")
    if not folder or not os.path.isdir(folder):
        return JSONResponse({"error": "no folder set"}, 400)
    if not q:
        return JSONResponse({"error": "query required"}, 400)

    results = []
    max_results = 100
    max_file_size = 500000  # 500KB
    ext_filter = ext.strip().lower().lstrip('.').split(',') if ext else []

    try:
        pattern = re.compile(re.escape(q), re.IGNORECASE)
    except:
        return JSONResponse({"error": "invalid regex"}, 400)

    for root, dirs, filenames in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith('.')]
        for fname in sorted(filenames):
            if len(results) >= max_results:
                break
            fext = os.path.splitext(fname)[1].lower().lstrip('.')
            # Skip binary extensions
            if fext in ('pyc', 'pyo', 'exe', 'dll', 'so', 'dylib', 'png', 'jpg', 'jpeg',
                        'gif', 'bmp', 'ico', 'woff', 'woff2', 'ttf', 'eot', 'zip', 'tar',
                        'gz', 'rar', '7z', 'mp3', 'mp4', 'avi', 'mov', 'pdf', 'db', 'sqlite'):
                continue
            if ext_filter and fext not in ext_filter:
                continue
            fpath = os.path.join(root, fname)
            if os.path.getsize(fpath) > max_file_size:
                continue
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                rel_path = os.path.relpath(fpath, folder)
                for i, line in enumerate(lines):
                    if len(results) >= max_results:
                        break
                    if pattern.search(line):
                        results.append({
                            "file": rel_path,
                            "line": i + 1,
                            "text": line.rstrip()[:200],
                            "col": line.lower().find(q.lower()) if q else 0
                        })
            except:
                continue
        if len(results) >= max_results:
            break

    return {"query": q, "results": results, "count": len(results), "truncated": len(results) >= max_results}


# ===== GIT OPERATIONS =====
def _run_git(folder, args, timeout=30):
    """Run a git command and return output."""
    if not folder or not os.path.isdir(folder):
        return {"error": "no folder"}, 400
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace"
        )
        return {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}, 500
    except FileNotFoundError:
        return {"error": "git not installed"}, 500
    except Exception as e:
        return {"error": str(e)}, 500

@router.get("/api/projects/{pid}/git/status")
def git_status(pid: str):
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    folder = projects[pid].get("folder", "")
    data, code = _run_git(folder, ["status", "--short"])
    return JSONResponse(data, code)

@router.get("/api/projects/{pid}/git/log")
def git_log(pid: str, n: int = 10):
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    folder = projects[pid].get("folder", "")
    data, code = _run_git(folder, ["log", f"-{n}", "--pretty=format:%H|%an|%ar|%s"])
    if data.get("stdout"):
        commits = []
        for line in data["stdout"].strip().split("\n"):
            parts = line.split("|", 3)
            if len(parts) >= 4:
                commits.append({"hash": parts[0][:8], "author": parts[1], "date": parts[2], "message": parts[3]})
        return JSONResponse({"commits": commits}, 200)
    return JSONResponse(data, code)

@router.post("/api/projects/{pid}/git/commit")
def git_commit(pid: str, data: dict):
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    folder = projects[pid].get("folder", "")
    message = data.get("message", "").strip()
    if not message:
        return JSONResponse({"error": "commit message required"}, 400)
    add_all = data.get("add_all", True)
    if add_all:
        _run_git(folder, ["add", "-A"])
    result, code = _run_git(folder, ["commit", "-m", message])
    return JSONResponse(result, code)

@router.post("/api/projects/{pid}/git/push")
def git_push(pid: str, data: dict = None):
    if data is None: data = {}
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    folder = projects[pid].get("folder", "")
    remote = data.get("remote", "origin")
    branch = data.get("branch", "main")
    result, code = _run_git(folder, ["push", remote, branch], timeout=60)
    return JSONResponse(result, code)

@router.post("/api/projects/{pid}/git/pull")
def git_pull(pid: str, data: dict = None):
    if data is None: data = {}
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    folder = projects[pid].get("folder", "")
    remote = data.get("remote", "origin")
    branch = data.get("branch", "main")
    result, code = _run_git(folder, ["pull", remote, branch], timeout=60)
    return JSONResponse(result, code)


# ===== PROJECT TEMPLATES =====
TEMPLATES = {
    "fastapi": {
        "name": "FastAPI Backend",
        "description": "Python FastAPI REST API",
        "files": {
            "main.py": "from fastapi import FastAPI\n\napp = FastAPI(title=\"My API\")\n\n@app.get(\"/\")\ndef root():\n    return {\"message\": \"Hello World\"}\n\nif __name__ == \"__main__\":\n    import uvicorn\n    uvicorn.run(app, host=\"0.0.0.0\", port=8000)\n",
            "requirements.txt": "fastapi>=0.100.0\nuvicorn>=0.20.0\n",
            "README.md": "# My FastAPI Project\n\nRun: `python main.py`\nAPI docs: http://localhost:8000/docs\n"
        }
    },
    "react": {
        "name": "React App",
        "description": "React single page application",
        "files": {
            "index.html": "<!DOCTYPE html>\n<html><head><title>My React App</title></head><body><div id=\"root\"></div><script src=\"./app.js\"></script></body></html>\n",
            "app.js": "function App() {\n  return React.createElement('div', null, React.createElement('h1', null, 'Hello React!'));\n}\nReactDOM.render(React.createElement(App), document.getElementById('root'));\n",
            "style.css": "body { font-family: sans-serif; margin: 0; padding: 20px; }\nh1 { color: #333; }\n",
            "README.md": "# My React App\n\nOpen index.html in browser.\n"
        }
    },
    "python-cli": {
        "name": "Python CLI",
        "description": "Command-line Python tool",
        "files": {
            "main.py": "#!/usr/bin/env python3\nimport argparse\n\ndef main():\n    parser = argparse.ArgumentParser(description=\"My CLI Tool\")\n    parser.add_argument('--name', default='World')\n    args = parser.parse_args()\n    print(f\"Hello, {args.name}!\")\n\nif __name__ == '__main__':\n    main()\n",
            "requirements.txt": "",
            "README.md": "# My CLI Tool\n\nUsage: `python main.py --name YourName`\n"
        }
    },
    "nextjs": {
        "name": "Next.js App",
        "description": "Next.js full-stack app",
        "files": {
            "package.json": '{\"name\":\"my-app\",\"version\":\"1.0.0\",\"scripts\":{\"dev\":\"next dev\",\"build\":\"next build\"},\"dependencies\":{\"next\":\"latest\",\"react\":\"latest\",\"react-dom\":\"latest\"}}',
            "README.md": "# My Next.js App\n\nSetup: `npm install`\nRun: `npm run dev`\nOpen: http://localhost:3000\n"
        }
    },
    "python-lib": {
        "name": "Python Library",
        "description": "Reusable Python package",
        "files": {
            "mylib/__init__.py": "from .core import hello\n\n__version__ = '0.1.0'\n",
            "mylib/core.py": "def hello(name='World'):\n    return f'Hello, {name}!'\n",
            "tests/test_core.py": "from mylib.core import hello\n\ndef test_hello():\n    assert hello('Test') == 'Hello, Test!'\n",
            "setup.py": "from setuptools import setup, find_packages\nsetup(name='mylib', version='0.1.0', packages=find_packages())\n",
            "README.md": "# My Python Library\n\nInstall: `pip install -e .`\nTest: `pytest`\n"
        }
    }
}

@router.get("/api/templates")
def list_templates():
    return [{"id": k, "name": v["name"], "description": v["description"]} for k, v in TEMPLATES.items()]

@router.get("/api/templates/{tid}")
def get_template(tid: str):
    t = TEMPLATES.get(tid)
    if not t:
        return JSONResponse({"error": "template not found"}, 404)
    return {"id": tid, **t}


# ===== PACKAGE MANAGER =====
@router.post("/api/projects/{pid}/packages/install")
def packages_install(pid: str, data: dict):
    """Install packages via pip or npm."""
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    folder = projects[pid].get("folder", "")
    if not folder:
        return JSONResponse({"error": "no folder set"}, 400)

    packages = data.get("packages", "").strip()
    manager = data.get("manager", "pip")  # pip or npm
    if not packages:
        return JSONResponse({"error": "packages list required"}, 400)

    pkg_list = [p.strip() for p in packages.split() if p.strip()]
    if not pkg_list:
        return JSONResponse({"error": "empty packages list"}, 400)

    try:
        if manager == "npm":
            cmd = ["npm", "install"] + pkg_list
            is_npm = True
        else:
            cmd = [sys.executable, "-m", "pip", "install"] + pkg_list
            is_npm = False

        result = subprocess.run(
            cmd, cwd=folder, capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace"
        )
        output = result.stdout + result.stderr
        success = result.returncode == 0

        # Save to requirements.txt if pip
        if manager == "pip" and success:
            try:
                req_file = os.path.join(folder, "requirements.txt")
                existing = set()
                if os.path.exists(req_file):
                    with open(req_file, "r") as f:
                        existing = {l.strip().split('==')[0].split('>=')[0].split('<=')[0].strip().lower() for l in f if l.strip() and not l.startswith('#')}
                with open(req_file, "a") as f:
                    for pkg in pkg_list:
                        pkg_name = pkg.split('==')[0].split('>=')[0].split('<=')[0].split('[')[0].strip().lower()
                        if pkg_name not in existing:
                            f.write(f"{pkg}\n")
            except:
                pass

        return {"success": success, "output": output[-2000:], "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "install timeout (120s)"}, 500)
    except FileNotFoundError:
        cmd_name = "npm" if manager == "npm" else "pip"
        return JSONResponse({"error": f"{cmd_name} not found"}, 500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)

@router.get("/api/projects/{pid}/packages/list")
def packages_list(pid: str, manager: str = "pip"):
    """List installed packages."""
    projects = _load_projects()
    if pid not in projects:
        return JSONResponse({"error": "not found"}, 404)
    folder = projects[pid].get("folder", "")
    try:
        if manager == "npm":
            pkg_file = os.path.join(folder, "package.json")
            if os.path.exists(pkg_file):
                with open(pkg_file, "r") as f:
                    data = json.load(f)
                deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                return {"packages": [{"name": k, "version": v} for k, v in deps.items()]}
            return {"packages": []}
        else:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "freeze"],
                capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace"
            )
            pkgs = []
            for line in result.stdout.strip().split("\n"):
                if "==" in line:
                    name, ver = line.split("==", 1)
                    pkgs.append({"name": name, "version": ver})
            return {"packages": pkgs}
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)
