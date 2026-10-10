# Third-party components

The MIT license in this repository covers Dots Brain source. Installed Python
packages retain the licenses distributed with those packages. Exact dependency
versions are recorded in `uv.lock`; model weights are downloaded separately and
are not included in Dots Brain release artifacts.

Optional embeddings use
[`qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q`](https://huggingface.co/qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q)
at revision `faf4aa4225822f3bc6376869cb1164e8e3feedd0`. This is the ONNX quantized
artifact used by the implementation, rather than the original training weights.
The [original Sentence Transformers model card](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/blob/main/README.md)
identifies its license as Apache-2.0. Preserve upstream license and notice files
when redistributing model artifacts or installed dependencies.
