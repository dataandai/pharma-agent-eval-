from pathlib import Path

from src.sandbox import Sandbox

ROOT = Path(__file__).resolve().parent
Sandbox(ROOT).reset()
print("Sandbox reset.")
