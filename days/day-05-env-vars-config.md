---
date: 2026-07-25
day: 05
title: "Environment Variables & Configuration Management"
tags: [env, config, secrets, python, dotenv, pydantic]
---

TL;DR
- Store secrets (API keys, tokens, DB passwords) in `.env` files, never hardcode them
- Use `python-dotenv` to load from `.env` files locally, and CI/CD secrets for production
- Validate config with Pydantic for type safety and defaults
- Never commit `.env` files to git—add to `.gitignore`

---

## 1. The Problem: Hardcoded Secrets

❌ **Bad:**
```python
# main.py
OPENAI_API_KEY = "sk-proj-abc123xyz789..."  # Leaked in GitHub!
GIGATOKEN_MODEL = "gpt2"
DATABASE_URL = "postgresql://user:password@localhost/db"

tokenizer = Tokenizer.from_pretrained(GIGATOKEN_MODEL)
```

**Risks:**
- Anyone with repo access sees your secrets
- If you push to GitHub, it's exposed forever (even after deletion)
- Accidentally committed secrets → security breach

✅ **Good:**
```python
# .env (local only, never committed)
OPENAI_API_KEY=sk-proj-abc123xyz789...
GIGATOKEN_MODEL=gpt2
DATABASE_URL=postgresql://user:password@localhost/db

# main.py (reads from environment)
import os
from dotenv import load_dotenv

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
```

---

## 2. Using `python-dotenv`

### Installation
```bash
pip install python-dotenv
```

### Setup

**Create `.env` file (local only):**
```bash
# .env
OPENAI_API_KEY=sk-proj-your-real-key-here
GIGATOKEN_MODEL=gpt2
DATABASE_URL=postgresql://user:password@localhost/mydb
DEBUG=True
MAX_TOKENS=2048
```

**Add to `.gitignore`:**
```bash
# .gitignore
.env
.env.local
*.env
```

**Load in Python:**
```python
from dotenv import load_dotenv
import os

# Load from .env file
load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")
model = os.getenv("GIGATOKEN_MODEL", "gpt2")  # default fallback
debug = os.getenv("DEBUG", "False") == "True"

print(f"Using model: {model}")
print(f"Debug mode: {debug}")
```

### Complete Example
```python
#!/usr/bin/env python3
from dotenv import load_dotenv
import os

# Load environment variables
load_dotenv()

# Get variables with defaults
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GIGATOKEN_MODEL = os.getenv("GIGATOKEN_MODEL", "gpt2")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///db.sqlite")
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
DEBUG = os.getenv("DEBUG", "False").lower() == "true"

# Validate critical vars
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY not set!")

print(f"✓ Config loaded:")
print(f"  Model: {GIGATOKEN_MODEL}")
print(f"  DB: {DATABASE_URL}")
print(f"  Retries: {MAX_RETRIES}")
print(f"  Debug: {DEBUG}")
```

---

## 3. Better: Pydantic for Validation

For production, use **Pydantic** for type checking + validation:

### Installation
```bash
pip install pydantic python-dotenv
```

### Configuration Class
```python
from pydantic import BaseSettings, Field
from typing import Optional

class Settings(BaseSettings):
    """Application configuration from .env"""
    
    # API & Model
    openai_api_key: str = Field(..., description="OpenAI API key (required)")
    gigatoken_model: str = Field(default="gpt2", description="Tokenizer model")
    
    # Database
    database_url: str = Field(default="sqlite:///db.sqlite")
    
    # Runtime
    max_tokens: int = Field(default=2048, ge=1, le=8192)
    debug: bool = Field(default=False)
    max_retries: int = Field(default=3, ge=1)
    
    class Config:
        env_file = ".env"  # Load from .env
        case_sensitive = False
        
        @classmethod
        def settings_customise_sources(cls, init_settings, env_settings, file_settings, settings_cls):
            """Priority: env vars > .env file > defaults"""
            return (
                init_settings,
                env_settings,
                file_settings,
            )

# Usage
settings = Settings()
print(f"Using model: {settings.gigatoken_model}")
print(f"Max tokens: {settings.max_tokens}")

if settings.debug:
    print("🔧 Debug mode ON")
```

### With Validation
```python
from pydantic import BaseSettings, validator, Field

class Settings(BaseSettings):
    openai_api_key: str
    gigatoken_model: str = "gpt2"
    database_url: str
    
    @validator("openai_api_key")
    def validate_api_key(cls, v):
        if not v.startswith("sk-"):
            raise ValueError("Invalid OpenAI API key format")
        if len(v) < 20:
            raise ValueError("API key too short")
        return v
    
    @validator("database_url")
    def validate_db_url(cls, v):
        if not v.startswith(("postgresql://", "sqlite://", "mysql://")):
            raise ValueError("Invalid database URL")
        return v
    
    class Config:
        env_file = ".env"

# This will raise validation errors if .env is missing required fields
settings = Settings()
```

---

## 4. Environment-Specific Configs

### Multiple Environments
```bash
# .env.local (development)
GIGATOKEN_MODEL=gpt2
DEBUG=True
DATABASE_URL=sqlite:///dev.db

# .env.prod (production template, committed to repo)
GIGATOKEN_MODEL=gpt2
DEBUG=False
DATABASE_URL=postgresql://user:password@prod.db.com/prod
```

### Load by Environment
```python
import os
from dotenv import load_dotenv

env = os.getenv("ENVIRONMENT", "local")

if env == "prod":
    load_dotenv(".env.prod")
else:
    load_dotenv(".env.local")

print(f"Running in {env} mode")
```

### With Pydantic
```python
from pydantic import BaseSettings
import os

class Settings(BaseSettings):
    environment: str = os.getenv("ENVIRONMENT", "local")
    debug: bool = False
    database_url: str
    
    class Config:
        env_file = f".env.{os.getenv('ENVIRONMENT', 'local')}"

settings = Settings()
print(f"Environment: {settings.environment}")
```

---

## 5. CI/CD Integration (GitHub Actions)

### GitHub Secrets
1. Go to repo → **Settings** → **Secrets and variables** → **Actions**
2. Add: `OPENAI_API_KEY`, `DATABASE_URL`, etc.

### Workflow Usage
```yaml
# .github/workflows/test.yml
name: Tests
on: [push]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      
      - name: Install deps
        run: pip install -r requirements.txt
      
      - name: Run tests
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
        run: pytest
```

---

## 6. Best Practices

✅ **DO:**
- Store `.env` in `.gitignore`
- Use unique keys per environment (dev ≠ prod)
- Rotate secrets regularly
- Document required env vars in `README.md`
- Use Pydantic for validation

❌ **DON'T:**
- Hardcode secrets in code
- Commit `.env` files
- Share `.env` over email/chat
- Use same secrets in dev and prod
- Print secrets to logs

### Document Required Config
```markdown
# .env.example (commit this to repo)
OPENAI_API_KEY=your-key-here
GIGATOKEN_MODEL=gpt2
DATABASE_URL=postgresql://localhost/db
DEBUG=False
MAX_TOKENS=2048
```

---

## 7. Quick Reference

```python
from dotenv import load_dotenv
import os

# Option 1: Simple (string values)
load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")

# Option 2: With defaults
model = os.getenv("GIGATOKEN_MODEL", "gpt2")

# Option 3: Type conversion
max_tokens = int(os.getenv("MAX_TOKENS", "2048"))
debug = os.getenv("DEBUG", "False") == "True"

# Option 4: Pydantic (recommended)
from pydantic import BaseSettings
class Settings(BaseSettings):
    class Config:
        env_file = ".env"
settings = Settings()  # Auto-loads & validates
```

---

## Links & Resources
- [python-dotenv docs](https://github.com/theskumar/python-dotenv)
- [Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
- [OWASP: Secrets Management](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html)
- [GitHub Secrets Docs](https://docs.github.com/en/actions/security-guides/encrypted-secrets)

---

## Next Steps / Reflections
- [ ] Add `.env.example` to your repos
- [ ] Refactor Day 1-4 projects to use environment variables
- [ ] Set up GitHub Secrets for a test workflow
- [ ] Try Pydantic config validation on existing projects
- [ ] Implement role-based configs (dev vs prod)
