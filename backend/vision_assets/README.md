# YOLO runtime checkpoint

`best.pt` is the supplied YOLO11s checkpoint, used directly by `backend/vision.py`.
The supplied `best (4).pt.zip` is a PyTorch checkpoint container, not a folder of
backend source code. Its bytes are copied unchanged with the `.pt` extension;
extracting the tensor container would prevent normal YOLO loading.

SHA-256: `79f2dd640499c490c54118b9d5a717a48ed5dbcc123d36a3713f70b8d49edc40`

The original upload remains in Downloads. Runtime does not need that upload or
the old `backend/weights` folder. Override the checkpoint with
`CLEANROUTEAI_YOLO_MODEL_PATH` when necessary.
