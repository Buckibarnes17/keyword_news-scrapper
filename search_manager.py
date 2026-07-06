import os

for root, dirs, files in os.walk("backend"):
    for file in files:
        if file.endswith(".py"):
            path = os.path.join(root, file)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                if "multiprocessing" in content or "Manager" in content:
                    print(f"Found in {path}")
            except Exception:
                pass
