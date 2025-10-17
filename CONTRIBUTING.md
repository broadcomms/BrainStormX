# Contributing to BrainStormX

## Clone the repo
```bash
git clone https://github.com/broadcomms/brainstorm_x.git
cd brainstorm_x
```

## Workflow

### 1. Always start by pulling the latest main.
```bash
git checkout main
git pull origin main
```

### 2. Create feature branch
```bash
git checkout -b feature/your-task
```
### 3. `.gitignore`
Ensure you’re not accidentally committing OS, IDE, or build artifacts.

### 4. Work, Commit and Push to branch
```bash
#edit files and push everything when done.
git add .
git commit -m "Write a short description of your changes"
git push --set-upstream origin feature/your-task
```

### 5. Open Pull request against main and ask for reviews.
```bash
#a. Make sure you're on your feature branch
git checkout feature/your-task

#b. Push your branch (if not already)
git push --set-upstream origin feature/your-task

#c. Create a pull request against main, requesting reviewer(s)
gh pr create \
  --base main \
  --head feature/your-task \
  --title "feat: add workshop room component" \
  --body "This adds a reusable display component to display the virtual facilitor agent response outputs" \
  --reviewer broadcomms
```

### 6. Merge after approval and CI check pass.
```bash
# checkout the pull request locally wheter 123 is your pull request number
gh pr checkout 123

# merge now with a merge commit
gh pr merge 123 --merge  

```

### 7. Delete the branch after merging (Optional)
```bash
gh pr merge 123 --delete-branch
```


