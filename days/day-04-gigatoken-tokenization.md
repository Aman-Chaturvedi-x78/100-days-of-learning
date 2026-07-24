---
date: 2026-07-24
day: 04
title: "Ultra-Fast LLM Tokenization with Gigatoken (24.53 GB/s)"
tags: [tokenization, rust, bpe, llm, performance, gigatoken]
---

TL;DR
- **Gigatoken** is a Rust-based BPE tokenizer achieving **24.53 GB/s**—**989x faster** than HuggingFace and **681x faster** than OpenAI's tiktoken
- Tokenization converts raw text → subword units (tokens) that LLMs understand
- Different models use different tokenizers (GPT-2, Llama 3, Qwen 3, DeepSeek V3, etc.)
- Rust's safety + zero-cost abstractions make it ideal for data pipeline optimization
- This bridges Day 1 (LLM sampling) and practical LLM inference

---

## 1. Why Tokenization Matters

### The LLM Pipeline

```
Raw Text → [TOKENIZATION] → Token IDs → LLM Model → Logits → [SAMPLING] → Output Tokens → Detokenization → Text
                ↑                                               ↑
            Day 4 Topic                                  Day 1 Topic
```

**The Problem**: Before Day 1's top-p/top-k sampling can happen, text must be converted to tokens. Traditional tokenizers (Python-based) are a **bottleneck**:
- HuggingFace Tokenizers: ~25 MB/s
- OpenAI tiktoken: ~36 MB/s
- **Gigatoken**: ~24,530 MB/s (24.53 GB/s)

### Why This Matters at Scale

When processing massive datasets:
- **1 GB corpus with HuggingFace**: ~40 seconds
- **1 GB corpus with Gigatoken**: ~0.04 seconds (1000x speedup!)
- Eliminates tokenization as a pipeline bottleneck

---

## 2. Understanding Tokenization: Byte-Pair Encoding (BPE)

### What Are Tokens?

LLMs don't understand words or characters. They understand **tokens**—subword units learned during training.

```
Text:      "Hello, world!"
Tokens:    ["Hello", ",", " world", "!"]
Token IDs: [15496,   11,   995,    0]
```

### Byte-Pair Encoding (BPE) Algorithm

BPE builds a vocabulary by iteratively merging the most frequent byte pairs:

**Step 1: Start with characters**
```
"hello" → ['h', 'e', 'l', 'l', 'o']
```

**Step 2: Merge most frequent pair (e.g., 'll')**
```
"hello" → ['h', 'e', 'll', 'o']
Vocabulary: {..., 'll': 256}
```

**Step 3: Repeat for full vocabulary (50K-200K tokens)**

### Tokenizer Differences

Each model has its own tokenizer trained on different data:

```python
# GPT-2 Tokenizer
import tiktoken
encoding = tiktoken.get_encoding("gpt2")
tokens = encoding.encode("Hello, world!")
# Output: [15496, 11, 995, 0]

# Llama 3 Tokenizer (different!)
# Output: [15496, 12, 1686, 0]  # Different token IDs for same text
```

**Why it matters**: Token count affects pricing and model behavior!

```
"Hello, world!" 
- GPT-2: 4 tokens
- Llama 3: 4 tokens
- Different algorithms: different boundaries
```

---

## 3. Gigatoken: Architecture & Performance

### Installation & Quick Start

```bash
# Install from PyPI
pip install gigatoken

# Or build from source (requires Rust)
git clone https://github.com/marcelroed/gigatoken
cd gigatoken
pip install -e .
```

### Basic Usage

```python
from gigatoken import Tokenizer

# Load tokenizer for GPT-2
tokenizer = Tokenizer.from_pretrained("gpt2")

# Tokenize
tokens = tokenizer.encode("Hello, world!")
print(tokens)  # [15496, 11, 995, 0]

# Decode back to text
text = tokenizer.decode(tokens)
print(text)  # "Hello, world!"
```

### Supported Tokenizers

Gigatoken supports 8+ tokenizer formats:
- ✅ GPT-2
- ✅ Llama 3 & 2
- ✅ Qwen 3
- ✅ DeepSeek V3
- ✅ GLM 5
- ✅ Kimi K2
- ✅ Nemotron 3

### Performance Comparison

| Tokenizer | Speed | Tokens/sec | Notes |
|-----------|-------|-----------|-------|
| HuggingFace | 25 MB/s | ~10M | Python, slower |
| tiktoken | 36 MB/s | ~14M | OpenAI's, faster C++ |
| **Gigatoken** | **24.53 GB/s** | **~10B** | Rust, **989x faster than HF** |

**Benchmark Setup**: GPT-2, Intel Xeon, 64 cores

---

## 4. Why Rust + Gigatoken is Superior

### The Rust Advantage

```rust
// Gigatoken (simplified Rust pseudocode)
pub fn encode_fast(text: &str, vocab: &HashMap<&str, u32>) -> Vec<u32> {
    // 1. Stack-allocated buffers (no GC pauses)
    // 2. SIMD operations for batch processing
    // 3. Zero-copy string slicing
    // 4. Compile-time safety checks
    // 5. No runtime overhead
}
```

**Why Rust wins**:
1. **No garbage collection pauses** — consistent microsecond latencies
2. **Memory efficiency** — zero-copy operations
3. **SIMD optimization** — processes multiple tokens in parallel
4. **Type safety** — catches bugs at compile time
5. **Concurrency** — safe multi-threaded tokenization

**Comparison with Python**:
```python
# Python (GC pauses, interpreted overhead)
def encode_python(text, vocab):  # 100x slower due to:
    # - Interpreter overhead
    # - Dynamic type checking
    # - Memory allocations
    # - GC pauses
    pass
```

---

## 5. Integration: Using Gigatoken in LLM Pipelines

### Pipeline Example

```python
from gigatoken import Tokenizer
import numpy as np

# 1. Initialize tokenizer
tokenizer = Tokenizer.from_pretrained("llama3")

# 2. Tokenize input
prompt = "Explain quantum computing in 2 sentences."
tokens = tokenizer.encode(prompt)
print(f"Input tokens: {len(tokens)}")  # e.g., 15 tokens

# 3. (Hypothetical) Load model and get logits
# logits = model(torch.tensor([tokens]))  # [1, 15, 128256]

# 4. Apply top-p sampling (from Day 1!)
def top_p_sample(logits, p=0.9, temperature=0.7):
    """Apply top-p (nucleus) sampling"""
    logits = logits / temperature
    probs = np.exp(logits) / np.sum(np.exp(logits))
    
    sorted_probs = np.sort(probs)[::-1]
    cumsum_probs = np.cumsum(sorted_probs)
    cutoff_idx = np.where(cumsum_probs > p)[0][0]
    cutoff_prob = sorted_probs[cutoff_idx]
    
    probs[probs < cutoff_prob] = 0
    probs = probs / np.sum(probs)
    
    next_token = np.random.choice(len(probs), p=probs)
    return next_token

# 5. Generate tokens
generated_tokens = []
for _ in range(50):  # Generate 50 tokens
    # next_token = model_inference(...)
    # next_token = top_p_sample(logits)
    # generated_tokens.append(next_token)
    pass

# 6. Detokenize (convert tokens → text)
# This is where tokenization speed matters!
output_text = tokenizer.decode(generated_tokens)
print(f"Generated: {output_text}")
```

### Batch Tokenization (Where Gigatoken Shines)

```python
# Tokenize 1000 documents at 24 GB/s
documents = [
    "Document 1 text...",
    "Document 2 text...",
    # ... 998 more
]

# Gigatoken: ~0.1ms
# HuggingFace: ~100ms
tokenized = [tokenizer.encode(doc) for doc in documents]
```

---

## 6. Advanced: Tokenizer Limitations & Gotchas

### Common Issues

**1. Token Count Mismatch**
```python
from gigatoken import Tokenizer
from transformers import AutoTokenizer

text = "Hello 👋 world!"

gigatoken_tokenizer = Tokenizer.from_pretrained("gpt2")
hf_tokenizer = AutoTokenizer.from_pretrained("gpt2")

# These might differ slightly:
print(len(gigatoken_tokenizer.encode(text)))  # 5
print(len(hf_tokenizer.encode(text)))          # 5
# Usually compatible, but test edge cases!
```

**2. Encoding Edge Cases**
```python
# Special tokens
text = "<|endoftext|>"
tokens = tokenizer.encode(text)
# Each tokenizer handles special tokens differently

# Unicode/Emoji
text = "Hello 世界 🌍"
tokens = tokenizer.encode(text)
# Behavior depends on vocab; may use multiple subword tokens
```

**3. Maximum Context Length**
```python
# GPT-2: 1024 token max context
# Llama 3: 8192 token max context
# Token count affects API costs!

text = "..." * 10000  # Very long text
tokens = tokenizer.encode(text)
if len(tokens) > 8192:
    print("Exceeds Llama 3 context window!")
    # Truncate or chunk text
    tokens = tokens[:8192]
```

---

## 7. Practical Exercise: Building a Token Counter

```python
#!/usr/bin/env python3
"""Token counter utility using Gigatoken"""

from gigatoken import Tokenizer
import sys
from pathlib import Path

def count_tokens_in_file(filepath: str, model: str = "gpt2") -> int:
    """Count tokens in a file"""
    tokenizer = Tokenizer.from_pretrained(model)
    
    with open(filepath, 'r', encoding='utf-8') as f:
        text = f.read()
    
    tokens = tokenizer.encode(text)
    return len(tokens)

def estimate_api_cost(token_count: int, model: str = "gpt-4") -> float:
    """Estimate OpenAI API cost"""
    # Approximate pricing (check OpenAI docs for current rates)
    rates = {
        "gpt-3.5-turbo": {"input": 0.0005, "output": 0.0015},
        "gpt-4": {"input": 0.03, "output": 0.06},
    }
    
    rate = rates.get(model, {"input": 0.01})
    cost = (token_count / 1000) * rate["input"]
    return cost

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python token_counter.py <filepath> [model]")
        sys.exit(1)
    
    filepath = sys.argv[1]
    model = sys.argv[2] if len(sys.argv) > 2 else "gpt2"
    
    if not Path(filepath).exists():
        print(f"Error: File '{filepath}' not found")
        sys.exit(1)
    
    token_count = count_tokens_in_file(filepath, model)
    cost = estimate_api_cost(token_count, "gpt-4")
    
    print(f"File: {filepath}")
    print(f"Tokens: {token_count:,}")
    print(f"Estimated GPT-4 cost: ${cost:.4f}")
```

**Run it:**
```bash
python token_counter.py myfile.txt gpt2
# Output:
# File: myfile.txt
# Tokens: 1,234
# Estimated GPT-4 cost: $0.0370
```

---

## 8. Key Takeaways

### What I Learned

1. **Tokenization is foundational** — every LLM interaction requires converting text → tokens
2. **Different models, different tokenizers** — GPT-2 ≠ Llama 3 ≠ Qwen 3 (different vocab/encoding)
3. **Performance matters at scale** — 989x speedup = seconds instead of hours on large datasets
4. **Rust dominates for data pipelines** — safety + speed + no GC pauses
5. **Token count = API cost** — understanding tokenization saves money
6. **BPE algorithm** — iteratively merges frequent byte pairs to build vocabulary

### Connection to Day 1

- **Day 1**: Learned about top-p vs top-k sampling (token generation)
- **Day 4**: Learned about tokenization (token creation)
- **Together**: Full LLM inference pipeline from text → tokens → generation → text

---

## Links & Resources

- **GitHub**: [marcelroed/gigatoken](https://github.com/marcelroed/gigatoken)
- **Paper**: [Neural Machine Translation by Jointly Learning to Align and Translate](https://arxiv.org/abs/1409.0473) — foundational for modern tokenization
- **BPE Algorithm**: [Neural Machine Translation of Rare Words with Subword Units](https://arxiv.org/abs/1508.07909) — Sennrich et al. (2016)
- **Benchmark**: [Gigatoken: Rust Tokenizer at 24.53 GB/s](https://www.marktechpost.com/2026/07/23/meet-gigatoken-a-rust-bpe-tokenizer-that-encodes-text-at-24-53-gb-s-up-to-989x-faster-than-huggingface-tokenizers/)
- **OpenAI tiktoken**: [tiktoken on GitHub](https://github.com/openai/tiktoken)
- **HuggingFace Tokenizers**: [HuggingFace Tokenizers](https://github.com/huggingface/tokenizers)

---

## Next Steps / Reflections

- [ ] Clone Gigatoken and experiment with different tokenizer formats
- [ ] Build a token counter tool for estimating API costs
- [ ] Benchmark Gigatoken vs HuggingFace on a large dataset
- [ ] Understand how special tokens (like `<|endoftext|>`) work
- [ ] Explore tokenizer training: how to build custom vocabularies
- [ ] Study the Rust source code to understand SIMD optimizations
- [ ] Create a mini-project: document analyzer that shows token distributions by model
