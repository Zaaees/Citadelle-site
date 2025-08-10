import os
import subprocess
from datetime import datetime

# 📂 Mets ici le chemin vers ton projet
REPO_PATH = r"F:\Users\freed\Desktop\Citadelle site\Citadelle-site"  

def run_cmd(cmd):
    """Exécute une commande shell et affiche sa sortie."""
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr)

def update_and_push():
    os.chdir(REPO_PATH)
    
    print("📥 Récupération des dernières modifications...")
    run_cmd("git pull")
    
    print("➕ Ajout des fichiers modifiés...")
    run_cmd("git add .")
    
    commit_message = f"Auto-update {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    print(f"💬 Commit : {commit_message}")
    run_cmd(f'git commit -m "{commit_message}"')
    
    print("📤 Envoi vers GitHub...")
    run_cmd("git push origin main")

if __name__ == "__main__":
    update_and_push()
