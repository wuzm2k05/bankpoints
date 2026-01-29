import logging
from logging.handlers import RotatingFileHandler
import config.config as config

_level_relations = {
  'debug':logging.DEBUG,
  'info':logging.INFO,
  'warning':logging.WARNING,
  'error':logging.ERROR,
  'critical':logging.CRITICAL
}

_init_logging_level = _level_relations.get(config.get_log_level().strip())
_logger = logging.getLogger("wxai")
_file_handler = RotatingFileHandler(config.get_log_file_name(), maxBytes=config.get_log_backup_file_size(), backupCount=config.get_log_backup_file_num(),encoding='utf-8')
_console_handler = logging.StreamHandler()

_console_handler.setLevel(_init_logging_level)
_file_handler.setLevel(_init_logging_level)
_logger.setLevel(_init_logging_level)

# Create a formatter
_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
_file_handler.setFormatter(_formatter)
_console_handler.setFormatter(_formatter)

# set log destination
_log_destination = config.get_log_destination()
_destinations = _log_destination.strip().split(',')
for des in _destinations:
  if des == "console":
    _logger.addHandler(_console_handler)
  elif des == "file":
    _logger.addHandler(_file_handler)

def get_logger():
  return _logger


# used for ops 
def change_log_level(level):
  _file_handler.setLevel(level)
  _console_handler.setLevel(level)
  _logger.setLevel(level)

