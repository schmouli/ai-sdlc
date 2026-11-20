"""
Configuration management for MetaTrader AutoTrading System
"""

import os
import json
from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Optional
import datetime
import zoneinfo


@dataclass
class TradingTimeConfig:
    """Trading time configuration for high volume periods"""
    high_volume_1_start: datetime.time
    high_volume_1_end: datetime.time
    high_volume_2_start: datetime.time 
    high_volume_2_end: datetime.time


@dataclass
class TradingDayConfig:
    """Trading day configuration"""
    weekend_days: List[int]  # Days when trading is not allowed (0=Monday, 6=Sunday)
    holiday_dates: List[str]  # Specific holiday dates in YYYY-MM-DD format
    custom_trading_schedule: Optional[Dict[str, bool]] = None  # Custom weekly schedule
    monday_start_time: Optional[datetime.time] = None  # Start time for Monday
    friday_end_time: Optional[datetime.time] = None  # End time for Friday


@dataclass
class StrategyConfig:
    """Strategy-specific configuration"""
    name: str
    symbol_file: str
    workbook_path: str
    worksheet_name: str
    trading_times: TradingTimeConfig
    default_lot_size: float = 0.1
    default_sl: int = 0
    default_tp: int = 0
    default_trade_count: int = 1


@dataclass
class SystemConfig:
    """System-wide configuration"""
    mt4_files_dir: str
    log_dir: str
    data_dir: str
    log_level: str = "INFO"
    log_format: str = "%(asctime)s %(levelname)s %(message)s"
    date_format: str = "%Y-%m-%d %H:%M:%S"
    timezone: str = "US/Eastern"
    trading_days: Optional[TradingDayConfig] = None
    trading_blackout_periods: Optional[Dict] = None


class ConfigManager:
    """Centralized configuration manager"""
    
    def __init__(self, environment: str = "development"):
        self.environment = environment
        self.base_dir = Path(__file__).parent.parent
        self.config_dir = self.base_dir / "config"
        
        # Load environment-specific config
        self.config = self._load_config()
        self.system_config = self._create_system_config()
        self.strategies = self._load_strategies()
    
    def _load_config(self) -> Dict:
        """Load configuration from JSON files"""
        json_file = self.config_dir / "environments" / f"{self.environment}.json"
        
        if not json_file.exists():
            raise FileNotFoundError(f"Configuration file not found: {json_file}")
        
        with open(json_file, 'r') as f:
            return json.load(f)
    
    def _create_system_config(self) -> SystemConfig:
        """Create system configuration object"""
        system_cfg = self.config.get('system', {})
        
        # Load trading day configuration
        trading_days_cfg = system_cfg.get('trading_days', {})
        trading_days = None
        if trading_days_cfg:
            # Parse start/end times if available
            monday_start = None
            if 'monday_start_time' in trading_days_cfg:
                try:
                    monday_start = datetime.time.fromisoformat(trading_days_cfg.get('monday_start_time'))
                except (ValueError, TypeError):
                    pass
                    
            friday_end = None
            if 'friday_end_time' in trading_days_cfg:
                try:
                    friday_end = datetime.time.fromisoformat(trading_days_cfg.get('friday_end_time'))
                except (ValueError, TypeError):
                    pass
            
            trading_days = TradingDayConfig(
                weekend_days=trading_days_cfg.get('weekend_days', [5, 6]),  # Default: Saturday, Sunday
                holiday_dates=trading_days_cfg.get('holiday_dates', []),
                custom_trading_schedule=trading_days_cfg.get('custom_trading_schedule'),
                monday_start_time=monday_start,
                friday_end_time=friday_end
            )
        
        return SystemConfig(
            mt4_files_dir=os.path.expanduser(system_cfg.get('mt4_files_dir', '')),
            log_dir=str(self.base_dir / system_cfg.get('log_dir', 'logs')),
            data_dir=str(self.base_dir / system_cfg.get('data_dir', 'data')),
            log_level=system_cfg.get('log_level', 'INFO'),
            log_format=system_cfg.get('log_format', '%(asctime)s %(levelname)s %(message)s'),
            date_format=system_cfg.get('date_format', '%Y-%m-%d %H:%M:%S'),
            timezone=system_cfg.get('timezone', 'US/Eastern'),
            trading_days=trading_days,
            trading_blackout_periods=system_cfg.get('trading_blackout_periods')
        )
    
    def _load_strategies(self) -> Dict[str, StrategyConfig]:
        """Load strategy configurations"""
        strategies = {}
        
        for strategy_name, strategy_cfg in self.config.get('strategies', {}).items():
            # Parse high volume trading times
            times_cfg = strategy_cfg.get('trading_times', {})
            trading_times = TradingTimeConfig(
                high_volume_1_start=datetime.time.fromisoformat(times_cfg.get('high_volume_1_start', '03:00:00')),
                high_volume_1_end=datetime.time.fromisoformat(times_cfg.get('high_volume_1_end', '04:00:00')),
                high_volume_2_start=datetime.time.fromisoformat(times_cfg.get('high_volume_2_start', '08:00:00')),
                high_volume_2_end=datetime.time.fromisoformat(times_cfg.get('high_volume_2_end', '12:00:00'))
            )
            
            strategies[strategy_name] = StrategyConfig(
                name=strategy_name,
                symbol_file=str(self.base_dir / strategy_cfg.get('symbol_file', '')),
                workbook_path=str(self.base_dir / strategy_cfg.get('workbook_path', '')),
                worksheet_name=strategy_cfg.get('worksheet_name', 'Trade'),
                trading_times=trading_times,
                default_lot_size=strategy_cfg.get('default_lot_size', 0.1),
                default_sl=strategy_cfg.get('default_sl', 0),
                default_tp=strategy_cfg.get('default_tp', 0),
                default_trade_count=strategy_cfg.get('default_trade_count', 1)
            )
        
        return strategies
    
    def get_strategy_config(self, strategy_name: str) -> Optional[StrategyConfig]:
        """Get configuration for a specific strategy"""
        return self.strategies.get(strategy_name)
    
    def get_ml_config(self, strategy_name: str = None) -> Dict:
        """Get ML configuration with optional strategy-specific overrides.
        
        Args:
            strategy_name: Optional strategy name ('dtdb', 'stme', 'vpower').
                          If provided, returns strategy-specific overrides merged with global defaults.
                          If None, returns global ML config.
        
        Returns:
            Dict containing ML configuration with strategy-specific overrides applied.
        """
        global_ml_config = self.config.get('system', {}).get('ml_trading', {})
        
        # If no strategy specified, return global config
        if not strategy_name:
            return global_ml_config.copy()
        
        # Start with global config as base
        ml_config = global_ml_config.copy()
        
        # Apply strategy-specific overrides if they exist
        strategy_overrides = global_ml_config.get('strategy_overrides', {})
        if strategy_name in strategy_overrides:
            # Update only the overridden keys, keep other global settings
            ml_config.update(strategy_overrides[strategy_name])
        
        return ml_config
    
    def get_live_ml_thresholds(self, strategy_name: str) -> Dict:
        """Get live ML thresholds: adaptive store first, JSON fallback.
        
        Phase 2: AdaptiveThresholdController persists tuned thresholds in
        SQLite (adaptive_thresholds table). This accessor prefers those live
        values and falls back to the JSON baseline (strategy_overrides) when
        no adaptation exists yet.
        
        Returns:
            Dict with min_win_probability, min_confidence, source
            ('adaptive' | 'json_baseline').
        """
        try:
            from src.ml.adaptive_thresholds import AdaptiveThresholdController
            live = AdaptiveThresholdController.get_instance(self).get_live_thresholds(
                strategy_name)
            return live
        except Exception:
            # Adaptive layer unavailable: pure JSON fallback
            ml_cfg = self.get_ml_config(strategy_name)
            return {
                'min_win_probability': float(
                    ml_cfg.get('min_win_probability', 0.60)),
                'min_confidence': float(ml_cfg.get('min_confidence', 0.45)),
                'source': 'json_baseline',
            }
    
    def get_all_strategies(self) -> List[str]:
        """Get list of all configured strategies"""
        return list(self.strategies.keys())
    
    def get_system_config(self) -> SystemConfig:
        """Get system configuration"""
        return self.system_config
    
    def get_timezone(self) -> zoneinfo.ZoneInfo:
        """Get configured timezone object"""
        return zoneinfo.ZoneInfo(self.system_config.timezone)


# Singleton instance
_config_manager = None

def get_config_manager(environment: str = None) -> ConfigManager:
    """Get singleton ConfigManager instance.

    If `environment` is not provided, prefer the METATRADER_ENV environment
    variable. If that is not set, fall back to 'production'.
    If the singleton already exists but a different environment is requested,
    the singleton is recreated with the new environment.
    """
    global _config_manager
    if environment is None:
        environment = os.environ.get('METATRADER_ENV', 'production')
    if _config_manager is None or _config_manager.environment != environment:
        _config_manager = ConfigManager(environment)
    return _config_manager