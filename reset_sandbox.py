from pathlib import Path
from src.data.repositories import SandboxRepository

ROOT = Path(__file__).resolve().parent
SandboxRepository(ROOT).reset()
print("Sandbox reset.")
