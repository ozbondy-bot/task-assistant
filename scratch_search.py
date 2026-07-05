import os

def search_files(directory, search_str):
    for root, dirs, files in os.walk(directory):
        if ".git" in dirs:
            dirs.remove(".git")
        for file in files:
            if file.endswith(".py"):
                filepath = os.path.join(root, file)
                # Skip bot/handlers files themselves to avoid noise
                if "bot\\handlers\\" in filepath or "bot/handlers/" in filepath:
                    continue
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        for i, line in enumerate(f, 1):
                            if search_str in line:
                                print(f"{filepath}:{i}: {line.strip()}")
                except Exception as e:
                    pass

if __name__ == "__main__":
    for module in ["chores", "tasks", "rewards", "shopping"]:
        print(f"--- Searching for {module} ---")
        search_files(".", module)
