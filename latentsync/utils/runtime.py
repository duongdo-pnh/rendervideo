"""Shared runtime flags for cooperative cancellation across the gradio UI and the pipeline.

The Process button sets/clears CANCEL; the long-running loops (diffusion chunks, GFPGAN batches)
check CANCEL.is_set() at each iteration and raise LatentSyncCancelled, which their try/except
unwinds cleanly (close writers, delete partial output). A Gradio sync function can't be killed
mid-CUDA-call, so this cooperative flag is what actually stops the GPU work (~one chunk later).
"""
import threading

CANCEL = threading.Event()


class LatentSyncCancelled(Exception):
    """Raised inside the chunk/batch loops when CANCEL is set, to abort a run cleanly."""
    pass
