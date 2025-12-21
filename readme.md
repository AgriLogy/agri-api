# Agrilogy Backend – Development & Contribution Guide

This repository contains the **Django backend** for the Agrilogy platform.

The project is designed to be **automated, consistent, and safe for collaboration**, especially for junior developers.  
Most rules here exist to **prevent mistakes before they reach production**.

Please read this document carefully before contributing.

---

## 🧠 Project Philosophy

We optimize for:

- Consistency over personal preferences
- Automation over manual steps
- Preventing bugs early (before deployment)
- Easy onboarding for new and junior developers
- Production-grade engineering standards

Formatting, linting, and deployment are **not optional**.

---

## 🌿 Branching Strategy

- `front` → Frontend  
  - Automatically deployed via **Vercel**
- `back` → Backend  
  - Automatically deployed to **DigitalOcean**

⚠️ **All backend development MUST be done on the `back` branch.**

Do not commit backend code to any other branch.

---

## 🧾 Commit Message Standards (MANDATORY)

This repository follows the **Conventional Commits** specification.

### Format

### Allowed types
- `feat:` → New feature
- `fix:` → Bug fix
- `chore:` → Tooling, formatting, config changes
- `refactor:` → Code restructuring without behavior change
- `docs:` → Documentation only
- `test:` → Tests only

### Examples
```text
feat: add soil moisture aggregation endpoint
fix: correct timezone conversion in alerts
chore: apply black and isort formatting
refactor: simplify alert calculation logic
docs: update backend setup instructions
