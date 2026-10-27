# GitHub Desktop Guide

A simple summary of the Starpack Dev Team workflow for making pull requests using GitHub Desktop — no terminal required.

## 1. Install & Setup (One-Time)

1. **Download GitHub Desktop** from [desktop.github.com](https://desktop.github.com) and install it.
2. **Sign in to GitHub**: `File → Options → Accounts → Sign in to GitHub.com` using the account Adrian invited you to.
3. **Clone the Starpack repo**: `File → Clone Repository → search "Starpack" → Clone`. You only do this once.

## 2. Starting a New Task

1. **Switch to `main` and update it**: click `Current Branch → main`, then `Fetch origin` and `Pull origin` if needed.
2. **Create a new branch** based on `main`: `Current Branch → New Branch`. Use names like:
   - `feature/add-cart-page`
   - `fix/login-bug`
   - `update/product-prices`
3. **Open the project in VS Code**: `Repository → Open in Visual Studio Code`.

## 3. Rebase Every Morning (Stay Synced)

Every morning before you start coding, rebase your branch onto `main` so you have the latest changes:

1. Make sure you're on **your task branch**: `Current Branch → select your branch`.
2. Click **`Branch → Rebase Current Branch…`** in the menu bar.
3. Select **`main`** from the list.
4. Click **`Rebase`**.
5. If there are conflicts, ask Adrian for help.

This keeps your branch up to date and avoids big merge conflicts later.

## 4. Saving Your Work (Commits)

1. **Review changes** in GitHub Desktop — click files to see green (added) / red (removed) lines.
2. **Write a commit message**:
   - **Summary** (required): short description, e.g., `"add product search feature"`
   - **Description** (optional): more details if needed
3. Click **`Commit to feature/your-task-name`**.

**Commit often!** Small, frequent commits are easier to review than one giant commit.

## 5. Pushing & Opening a Pull Request

1. **Push your commits**: click `Publish branch` (first time) or `Push origin` (after).
2. **Create the Pull Request**: click `Create Pull Request` — this opens GitHub in your browser.
3. Fill in a **title** and **description**, then click the green **Create pull request** button.

**CodeRabbit** (AI code reviewer) will review your PR within a few minutes and post comments.

## 6. Fixing Review Comments (Same PR!)

**Important:** Do NOT create a new branch or new PR for review fixes.

1. Read comments on your PR on github.com.
2. Make sure `Current Branch` in GitHub Desktop is still your task branch.
3. Fix the code in VS Code.
4. Commit with a message like `"fix: address review comments"`.
5. Click **`Push origin`** — your PR updates automatically and CodeRabbit re-reviews.

Repeat until everything is clean.

## 7. After Your PR Is Merged

1. Switch back to `main` in GitHub Desktop.
2. Click `Fetch origin`, then `Pull origin`.
3. Create a new branch for your next task.
4. Optional cleanup: `Branch → Delete → check "Yes, delete on remote" → Delete`.

## 8. Daily Workflow Summary

| Step | Action | Where |
|------|--------|-------|
| 1 | Switch to `main`, Fetch + Pull | GitHub Desktop |
| 2 | Create new branch (or rebase existing branch onto `main`) | GitHub Desktop |
| 3 | Write code | VS Code |
| 4 | Commit changes (often!) | GitHub Desktop |
| 5 | Push to GitHub | GitHub Desktop |
| 6 | Create Pull Request | GitHub Desktop → Browser |
| 7 | Read CodeRabbit comments | Browser |
| 8 | Fix code, commit, push again | VS Code → GitHub Desktop |
| 9 | Repeat 7–8 until clean | — |
| 10 | Adrian merges → Done! | — |

## Rules

1. **NEVER push directly to `main`.** Always use a branch + Pull Request.
2. **ONE branch per task.** Don't mix multiple tasks in one branch.
3. **Read CodeRabbit's comments** and fix them before requesting Adrian's review.
4. **Commit often** — small commits are easier to review.
5. **Never create a new PR** to fix review comments. Push to the same branch.
6. **Rebase every morning.** Before you start coding, rebase your branch onto `main` to stay synced.
7. **If you're stuck, ask.** Don't spend hours going in circles.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "I can't push — it says rejected" | Click `Fetch origin`, then `Pull origin`, then try pushing again. |
| "I accidentally committed to `main`" | Tell Adrian immediately. Do NOT push — he'll help move the commit. |
| "I don't see my repo" | `File → Clone Repository` and clone it again. |
| "GitHub Desktop says there are conflicts" | Your code overlaps with someone else's changes. Ask Adrian for help. |
| "CodeRabbit isn't reviewing my PR" | Wait 3–5 minutes. If still nothing, ask Adrian to check the install. |
| "I want to undo my last commit" | Right-click the commit in History → `Undo commit`. |
