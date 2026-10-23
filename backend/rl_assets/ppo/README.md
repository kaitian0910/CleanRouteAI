# Extracted PPO runtime model

- `policy.pth`: original trained actor and critic state dictionary, extracted unchanged from the supplied PPO archive.
- `model.json`: explicit architecture and input/output configuration used by `backend/rl_policy.py`.

The loader reconstructs the original Tanh network and loads the weights directly. No optimizer state or serialized Python metadata is required for runtime inference. The original ZIP remains in Downloads and is not required by the project. The observation encoder, dispatch restrictions, deterministic planner and feedback orchestration remain in the backend Python modules.
