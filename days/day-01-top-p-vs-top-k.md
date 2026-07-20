---
date: 2026-07-20
day: 01
title: "Top-p (nucleus) sampling vs Top-k sampling (code)"
tags: [ml, sampling, transformers, code]
---

TL;DR
- Top-k: sample only from the k most probable tokens (fixed budget).
- Top-p (nucleus): sample from the smallest set of tokens whose cumulative probability ≥ p (adaptive).

Improved nucleus-sampling implementations (keeps token indices and renormalizes)

NumPy implementation
```python
import numpy as np

def softmax(x):
    e = np.exp(x - np.max(x))
    return e / e.sum()

def nucleus_sampling(logits, p=0.9):
    """Return a sampled token index (original vocab index) using nucleus (top-p) sampling.

    Args:
        logits: 1D array-like of unnormalized logit scores for the vocabulary.
        p: cumulative probability threshold (e.g., 0.9).
    """
    probs = softmax(np.asarray(logits))
    # sort probs descending and keep original token indices
    sorted_idx = np.argsort(-probs)
    sorted_probs = probs[sorted_idx]
    cumsum = np.cumsum(sorted_probs)
    # find smallest prefix with cumulative >= p
    cutoff = np.searchsorted(cumsum, p)
    nucleus_idx = sorted_idx[: cutoff + 1]
    nucleus_probs = probs[nucleus_idx]
    # renormalize within the nucleus
    nucleus_probs = nucleus_probs / nucleus_probs.sum()
    # sample a token index (returns the original token index)
    return np.random.choice(nucleus_idx, p=nucleus_probs)
```

PyTorch implementation
```python
import torch
import torch.nn.functional as F

def nucleus_sampling_torch(logits, p=0.9):
    """Assumes 1D logits tensor. Returns an int token index in the original vocab."""
    probs = F.softmax(logits, dim=-1)
    sorted_probs, sorted_idx = torch.sort(probs, descending=True)
    cumsum = torch.cumsum(sorted_probs, dim=-1)
    # cutoff is the first index where cumsum >= p
    cutoff = torch.searchsorted(cumsum, torch.tensor(p, device=cumsum.device))
    # cutoff is a tensor; convert to int
    cutoff = cutoff.item() if isinstance(cutoff, torch.Tensor) else int(cutoff)
    nucleus_idx = sorted_idx[: cutoff + 1]
    nucleus_probs = probs[nucleus_idx]
    nucleus_probs = nucleus_probs / nucleus_probs.sum()
    choice = torch.multinomial(nucleus_probs, num_samples=1)
    return nucleus_idx[choice].item()
```

Notes
- Both implementations:
  - Keep original token indices so the returned value maps to the vocabulary.
  - Use a numerically stable softmax (subtract max).
  - Use searchsorted to find the smallest prefix meeting the cumulative threshold p.
  - Renormalize probabilities inside the selected nucleus before sampling.
- The PyTorch version shown assumes 1D logits; for batched inputs you can vectorize the searchsorted/cutoff logic or process batch elements in a loop.

References
- Holtzman et al., "The Curious Case of Neural Text Degeneration" (2020) — nucleus sampling.
- Hugging Face transformers docs for practical sampling utilities.
