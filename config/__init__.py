"""
MetaTrader AutoTrading System Configuration
"""
import os
import json
from pathlib import Path
from typing import Dict, Any, Optional

# Base project directory
PROJECT_ROOT = Path(__file__).parent.parent

# Data directories
DATA_DIR = PROJECT_ROOT / "data"
EXPORTS_DIR = DATA_DIR / "exports"
SAMPLES_DIR = DATA_DIR / "samples"
SYMBOLS_DIR = DATA_DIR / "symbols"

# Output directories
OUTPUT_DIR = PROJECT_ROOT / "output"
LOGS_DIR = PROJECT_ROOT / "logs"

# Configuration directories
CONFIG_DIR = PROJECT_ROOT / "config"
ENVIRONMENTS_DIR = CONFIG_DIR / "environments"

# Database configuration
DATABASE_PATH = DATA_DIR / "trade_analytics.db"

# Default trading configuration
DEFAULT_CONFIG = {
    "trading": {
        "max_positions": 10,
        "max_risk_per_trade": 0.02,
        "default_stop_loss": 50,
        "default_take_profit": 100,
        "kelly_multiplier": 1.0
    },
    "logging": {
        "level": "INFO",
        "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        "file_level": "DEBUG",
        "console_level": "INFO"
    },
    "database": {
        "path": str(DATABASE_PATH),
        "backup_interval": 3600,  # seconds
        "auto_vacuum": True
    }
}

class ConfigManager:
    """Configuration management for the trading system"""
    
    def __init__(self, environment: str = "development"):
        """
        Initialize configuration manager
        
        Args:
            environment: Environment name (development, production, testing)
        """
        self.environment = environment
        self._config = None
        self._load_config()
    
    def _load_config(self):
        """Load configuration from environment-specific file"""
        config_file = ENVIRONMENTS_DIR / f"{self.environment}.json"
        
        if config_file.exists():
            with open(config_file, 'r') as f:
                env_config = json.load(f)
            
            # Merge with default config
            self._config = self._merge_configs(DEFAULT_CONFIG, env_config)
        else:
            self._config = DEFAULT_CONFIG.copy()
    
    def _merge_configs(self, base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
        """Recursively merge configuration dictionaries"""
        result = base.copy()
        
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_configs(result[key], value)
            else:
                result[key] = value
        
        return result
    
    def get(self, key: str, default: Any = None) -> Any:
        """
        Get configuration value using dot notation
        
        Args:
            key: Configuration key (e.g., 'trading.max_positions')
            default: Default value if key not found
            
        Returns:
            Configuration value
        """
        keys = key.split('.')
        value = self._config
        
        try:
            for k in keys:
                value = value[k]
            return value
        except (KeyError, TypeError):
            return default
    
    def set(self, key: str, value: Any):
        """
        Set configuration value using dot notation
        
        Args:
            key: Configuration key (e.g., 'trading.max_positions')
            value: Value to set
        """
        keys = key.split('.')
        config = self._config
        
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]
        
        config[keys[-1]] = value
    
    def save(self):
        """Save current configuration to environment file"""
        config_file = ENVIRONMENTS_DIR / f"{self.environment}.json"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        
        with open(config_file, 'w') as f:
            json.dump(self._config, f, indent=2)
    
    @property
    def config(self) -> Dict[str, Any]:
        """Get full configuration dictionary"""
        return self._config.copy()


# Global configuration instance
_config_manager: Optional[ConfigManager] = None

def get_config(environment: Optional[str] = None) -> ConfigManager:
    """
    Get global configuration manager
    
    Args:
        environment: Environment to use (if not already set)
        
    Returns:
        ConfigManager instance
    """
    global _config_manager
    
    if _config_manager is None or (environment and _config_manager.environment != environment):
        env = environment or os.getenv("TRADING_ENV", "development")
        _config_manager = ConfigManager(env)
    
    return _config_manager


# Convenience functions
def get_trading_config() -> Dict[str, Any]:
    """Get trading configuration"""
    return get_config().get("trading", {})

def get_database_path() -> str:
    """Get database path"""
    return get_config().get("database.path", str(DATABASE_PATH))

def get_log_level() -> str:
    """Get logging level"""
    return get_config().get("logging.level", "INFO")

def ensure_directories():
    """Ensure all required directories exist"""
    directories = [
        DATA_DIR,
        EXPORTS_DIR,
        SAMPLES_DIR,
        SYMBOLS_DIR,
        OUTPUT_DIR,
        LOGS_DIR,
        CONFIG_DIR,
        ENVIRONMENTS_DIR
    ]
    
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)


# Initialize directories when module is imported
ensure_directories()