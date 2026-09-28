"""meetcap: auto-record meetings on macOS, push to Azure, transcribe, summarize.

Public SDK surface (UIs and the CLI use only these):
    MeetCapAgent  - detection + recording + background processing
    Pipeline      - processing stages for one recording
    JobStore      - durable job state
    load_config   - configuration
"""
__version__ = "0.8.0"

__all__ = ["__version__", "MeetCapAgent", "Pipeline", "JobStore", "load_config"]


def __getattr__(name):  # lazy imports keep `import meetcap` cheap and dependency-light
    if name == "MeetCapAgent":
        from .agent import MeetCapAgent
        return MeetCapAgent
    if name == "Pipeline":
        from .pipeline import Pipeline
        return Pipeline
    if name == "JobStore":
        from .jobs import JobStore
        return JobStore
    if name == "load_config":
        from .config import load_config
        return load_config
    raise AttributeError(name)
