import sys
from pathlib import Path

# Add src/ to path so tests can import rag, agent, etc. directly
sys.path.insert(0, str(Path(__file__).parent / "src"))
