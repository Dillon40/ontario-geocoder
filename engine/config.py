"""
Configuration module for loading JSON configs used by the engine.
"""
import json
from pathlib import Path
from typing import Dict, Any


# Get config directory (relative to project root)
CONFIG_DIR = Path(__file__).parent.parent / 'config'

# StatCan province code for Ontario
ONTARIO_PROV_CODE = 35


def load_json_config(filename: str) -> Dict[str, Any]:
    """
    Load a JSON configuration file.

    Args:
        filename: Name of the config file

    Returns:
        Configuration dictionary
    """
    config_path = CONFIG_DIR / filename
    with open(config_path, 'r') as f:
        return json.load(f)


# Load configuration files needed by the engine
dataset_mappings = load_json_config('dataset-mappings.json')
street_type_mappings = load_json_config('street-type-mappings.json')
direction_mappings = load_json_config('direction-mappings.json')
