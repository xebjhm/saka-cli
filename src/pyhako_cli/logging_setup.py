
import logging
import sys
import os
import structlog
from tqdm import tqdm

class TqdmStream:
    """A file-like object that redirects write to tqdm.write."""
    def write(self, message: str) -> None:
        # structlog (or PrintLoggerFactory) adds a newline. tqdm.write also adds one.
        # We strip one to avoid double spacing.
        tqdm.write(message.rstrip())

    def flush(self) -> None:
        pass

def setup_logging(verbose: bool = False):
    """
    Configure structlog AND standard logging for the application.
    
    Args:
        verbose (bool): If True, set log level to DEBUG and enable library logs.
    """
    
    # 1. Standard Logging Configuration
    # We want standard logging to handle levels/filtering
    
    log_level = logging.DEBUG if verbose else logging.INFO
    
    log_level = logging.DEBUG if verbose else logging.INFO
    
    
    # 2. Structlog Configuration (Override or Supplement)
    # Even if PyHako configured structlog with cache=True, we can control RENDERING via the Handler Formatter.
    
    # Define the renderers we want (Console vs JSON)
    # Logic: If JSON_LOGS=1 -> JSON. Else -> Console.
    use_json = bool(os.environ.get("JSON_LOGS"))
    timestamp_fmt = "iso" if use_json else "%H:%M:%S"
    
    render_processor = structlog.processors.JSONRenderer() if use_json else structlog.dev.ConsoleRenderer()
    
    # Configure the formatter that will process the log entries from stdlib
    formatter = structlog.stdlib.ProcessorFormatter(
        # These run on log entries that did NOT originate from structlog (e.g. library logs)
        foreign_pre_chain=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt=timestamp_fmt),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
        ],
        # These run on ALL log entries (after foreign_pre_chain or after structlog processors)
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            render_processor,
        ],
    )

    # TQDM-safe Handler for Console (or standard StreamHandler)
    # We always replace the root handlers to ensure we own the output format.
    
    # Clean slate: Remove existing handlers
    root = logging.getLogger()
    if root.handlers:
        for h in root.handlers:
            root.removeHandler(h)
            
    # Add our handler
    handler = logging.StreamHandler(TqdmStream() if sys.stdout.isatty() else sys.stdout)
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(log_level)


    # Re-configure structlog just in case it wasn't configured (unlikely if PyHako imported)
    # But if it WAS configured with cache=True, this might happen too late for existing loggers.
    # That's why we rely on the ProcessorFormatter in the handler to do the final rendering.
    try:
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                structlog.contextvars.merge_contextvars,
                structlog.stdlib.add_logger_name,
                structlog.stdlib.add_log_level,
                structlog.processors.TimeStamper(fmt=timestamp_fmt),
                structlog.processors.StackInfoRenderer(),
                structlog.dev.set_exc_info,
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )
    except Exception:
        pass  # Ignore if already configured/immutable issues, as Handler handles rendering.

    # 3. Third-party Library Noise Reduction
    # Unless verbose, silence library info logs
    if not verbose:
        # PyHako Library: Only WARNING+
        logging.getLogger("pyhako").setLevel(logging.WARNING)
        # HTTP Libraries: Only WARNING+
        logging.getLogger("aiohttp").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        
        # PyHakoCLI (This app): Keep INFO
        logging.getLogger("pyhako_cli").setLevel(logging.INFO)
    else:
        # In verbose mode, let everything spill
        logging.getLogger("pyhako").setLevel(logging.DEBUG)
        logging.getLogger("pyhako_cli").setLevel(logging.DEBUG)
