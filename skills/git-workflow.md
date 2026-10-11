---
name: git-workflow
mode: dev
description: Safe local git workflow - branch, commit, review diff, and push.
---
1. Run git status and git diff to see what changed before committing.
2. Create a feature branch: git switch -c feature/<short-name>.
3. Stage deliberately (git add -p) and write imperative, descriptive commit messages.
4. Never force-push or hard-reset without telling the user exactly what will be lost.
5. Push with git push -u origin <branch> and summarize the changes.
