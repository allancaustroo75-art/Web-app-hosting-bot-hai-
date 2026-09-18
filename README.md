# SNUKED HOSTER — Single File Backend Hosting Platform

> Premium bot hosting with Telegram authentication. Extremely simple Railway deployment via single `app.py`.

## 🚀 Railway Deployment (Single File)

### Deploy with one command:
```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

### 1. Edit `app.py` configuration at top:
```python
BOT_TOKEN = "PASTE_BOT_TOKEN_HERE"
OWNER_USER_ID = 123456789
ADMIN_USER_IDS = []
SECRET_KEY = "PASTE_RANDOM_SECRET_HERE"
MINI_APP_URL = "https://YOUR-RAILWAY-DOMAIN"
CORS_ORIGINS = ["https://YOUR-RAILWAY-DOMAIN"]
```

### 2. Railway setup:
- Create project from GitHub repo
- Railway will auto-detect `requirements.txt` and run `uvicorn app:app`
- Or set Start Command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
- Set env vars in Railway dashboard (optional, overrides hard-coded config):
  ```
  BOT_TOKEN=123456:AAH...
  OWNER_USER_ID=111111111
  SECRET_KEY=long-random-string
  MINI_APP_URL=https://your-app.up.railway.app
  ```

### 3. Frontend (Netlify):
- Deploy `frontend/` folder to Netlify
- Edit `frontend/js/config.js`:
  ```javascript
  const API_BASE_URL = "https://your-app.up.railway.app";
  ```
- Or set via localStorage: `localStorage.setItem('api_base_url', 'https://your-backend.railway.app')`

## ✨ Features (Preserved)

### 🔐 Security
- Telegram `initData` server-side validation (HMAC-SHA256 with BOT_TOKEN)
- Never trusts `initDataUnsafe`
- Owner/admin authorization enforced on backend
- Path traversal prevention
- User isolation - users can only access own projects
- Owner can see all users, all projects, edit files, start/stop/restart, view logs
- Secrets never exposed to frontend/API

### 📦 Hosting
- Python & Node.js support (auto-detects runtime)
- Per-project isolated venv
- Auto-install requirements.txt & package.json
- Start/Stop/Restart with resource limits
- Live logs (tail, clear, copy, download)

### 👑 Owner Hierarchy
- **Owner:** Full global access - all users, all projects, file manager, controls, logs
- **Normal users:** Only own projects
- Telegram profile shows ONLY @username + PFP (no first/last name)
- Backend uses verified numeric user_id for authorization

### 💎 Premium UI
- Dark theme, mobile-first Telegram Mini App
- Pages: Home, Projects, Project Detail, File Manager, Editor, Logs, Create, Activity, Profile, Owner Panel
- Owner Panel: Stats (Total Users, Projects, Running, Stopped), Users list (photo+@username), User→Projects, All Projects

## 📁 Project Structure (Single File Backend)

```
WEB-HOSTING/
├── app.py                 # SINGLE FILE BACKEND - All functionality merged
├── requirements.txt       # All dependencies for app.py
├── frontend/              # Static frontend for Netlify
│   ├── index.html
│   ├── css/styles.css
│   └── js/
│       ├── config.js      # API base URL config for Netlify + Railway
│       ├── telegram.js    # Telegram WebApp helper
│       └── app.js         # SPA logic
├── data/                  # SQLite DB (persistent)
├── projects/              # User projects (persistent)
└── README.md
```

**No longer required:**
- ❌ `.env` file
- ❌ `start.sh`
- ❌ `backend/` folder with multiple modules
- ❌ `python-dotenv`

## 🔧 Configuration Inside app.py

```python
BOT_TOKEN = "PASTE_BOT_TOKEN_HERE"
OWNER_USER_ID = 123456789
ADMIN_USER_IDS = []
SECRET_KEY = "PASTE_RANDOM_SECRET_HERE"
MINI_APP_URL = "https://YOUR-RAILWAY-DOMAIN"
CORS_ORIGINS = ["https://YOUR-RAILWAY-DOMAIN"]
DATABASE_PATH = "./data/bothost.db"
PROJECTS_DIR = "./projects"
```

Railway env vars override these if set (for compatibility).

## 📡 API Endpoints (All preserved)

```
POST   /api/auth/telegram
GET    /api/auth/me
POST   /api/auth/token-login
POST   /api/auth/logout
GET    /api/me
GET    /api/activity
GET    /api/projects
POST   /api/projects
GET    /api/projects/{id}
POST   /api/projects/{id}/start
POST   /api/projects/{id}/stop
POST   /api/projects/{id}/restart
DELETE /api/projects/{id}
GET    /api/projects/{id}/files
POST   /api/projects/{id}/files
GET    /api/projects/{id}/files/{path}
PUT    /api/projects/{id}/files/{path}
POST   /api/projects/{id}/files/{path}/rename
DELETE /api/projects/{id}/files/{path}
GET    /api/projects/{id}/logs
DELETE /api/projects/{id}/logs

# Owner only
GET    /api/admin/stats
GET    /api/admin/users
GET    /api/admin/users/{user_id}/projects
GET    /api/admin/projects
GET    /api/admin/projects/{id}
DELETE /api/admin/projects/{id}
POST   /api/admin/projects/{id}/start|stop|restart
GET    /api/admin/projects/{id}/files
GET    /api/admin/projects/{id}/files/{path}
PUT    /api/admin/projects/{id}/files/{path}
POST   /api/admin/projects/{id}/files
DELETE /api/admin/projects/{id}/files/{path}
POST   /api/admin/projects/{id}/files/{path}/rename
GET    /api/admin/projects/{id}/logs
DELETE /api/admin/projects/{id}/logs

WS     /ws/projects/{id}/logs
GET    /health
GET    /api/health
GET    /api/runtimes
```

## 🌐 Frontend Config for Netlify

Edit `frontend/js/config.js`:
```javascript
const API_BASE_URL = "https://YOUR-RAILWAY-BACKEND.up.railway.app";
```

Frontend will communicate with Railway backend via this URL. CORS is configured in app.py.

## 🧪 Local Testing

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 - API only. Deploy frontend to Netlify or serve frontend folder separately.

## ⚠️ Security Notes

- Uploaded code runs as subprocess with resource limits (Linux rlimits)
- Set strong SECRET_KEY
- Configure CORS_ORIGINS to exact domains in production
- Keep PROJECTS_DIR outside web root
- BOT_TOKEN and SECRET_KEY never sent to frontend

## 📜 License

MIT
