"""
Logging Configuration
"""

import logging
import logging.handlers
from pathlib import Path
from typing import Optional


def setup_logger(
    name: str, 
    log_file: Optional[Path] = None, 
    level: str = "INFO",
    max_bytes: int = 10 * 1024 * 1024,  # 10MB
    backup_count: int = 5
) -> logging.Logger:
    """
    Set up a logger with file and console handlers
    
    Args:
        name: Logger name
        log_file: Path to log file (optional)
        level: Logging level
        max_bytes: Maximum log file size before rotation
        backup_count: Number of backup log files to keep
        
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    
    # Don't add handlers if logger already has them
    if logger.handlers:
        return logger
    
    # Convert string level to logging level
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(numeric_level)
    
    # Create formatter
    formatter = logging.Formatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # File handler (if log file specified)
    if log_file:
        # Create log directory if it doesn't exist
        log_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Use rotating file handler
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8'
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


def get_logger(name: str) -> logging.Logger:
    """
    Get an existing logger by name

    Args:
        name: Logger instance name

    Returns:
        logging.Logger: The logger
    """
    return logging.getLogger(name)


def ensure_file_handler(logger_name: str,
                        log_file,
                        level: str = "INFO",
                        max_bytes: int = 10 * 1024 * 1024,
                        backup_count: int = 5) -> logging.Logger:
    """
    Attach a rotating file handler to an existing logger if it has none.

    Execution-path loggers (TradingEngine, MT4FileConnector, ResponseHandler,
    PortfolioRiskGate) historically had no file handler, so trade failures
    (risk-gate blocks, command-send errors, MT4 error responses) were only
    visible on a transient console — impossible to diagnose after the fact.
    This helper wires them to `log_file` idempotently: calling it repeatedly
    (engine constructed per trade) never duplicates handlers for the same
    target file.

    Args:
        logger_name: Name of the logger to wire (e.g. "TradingEngine.NZDUSD")
        log_file: Path object or string for the log file
        level: Handler level (logger level is left untouched)
        max_bytes: RotatingFileHandler max bytes
        backup_count: RotatingFileHandler backup count

    Returns:
        logging.Logger: The wired logger
    """
    import os
    logger = logging.getLogger(logger_name)
    if log_file is None:
        return logger
    target = os.path.abspath(str(log_file))
    for h in logger.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler) \
                and os.path.abspath(h.baseFilename) == target:
            return logger  # already wired to this exact file

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    # Also raise the logger itself when it would filter INFO records (an
    # unwired logger inherits root's WARNING default, which silently drops
    # the success-path execution lines this handler exists to capture).
    if logger.level == logging.NOTSET or logger.level > numeric_level:
        logger.setLevel(numeric_level)
    file_handler = logging.handlers.RotatingFileHandler(
        str(log_path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8'
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(logging.Formatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(file_handler)
    return logger


def set_log_level(logger_name: str, level: str):
    """
    Set log level for a specific logger
    
    Args:
        logger_name: Name of the logger
        level: New log level
    """
    logger = logging.getLogger(logger_name)
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(numeric_level)
    
    # Update handler levels too
    for handler in logger.handlers:
        handler.setLevel(numeric_level)