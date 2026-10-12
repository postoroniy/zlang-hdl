# Maintained regression reproducers

These `.zhl` files are small compiler regression inputs retained by permanent
tests. They are not user tutorials and do not describe a separate language
surface.

| Source | Regression boundary |
| --- | --- |
| `ifft_identifier.zhl` | Callable/generic identifier resolution in the original reduced IFFT case. |
| `ifft_complex_reduce.zhl` | Exact nominal complex reduction and generated generics. |
| `ifft64_whole_vector_elaboration.zhl` | Bounded whole-vector IFFT elaboration and functional-region lowering. |
| `ifft_sdf_exact_feedback_type_growth.zhl` | Exact feedback arithmetic type growth in the SDF shape. |
| `stdlib_generic_storage_blockers.zhl` | Generic standard-library storage specialization diagnostics. |

Move a reproducer only with its owning tests. Once a defect is fixed, the input
remains useful evidence; it should not be deleted merely because compilation
now succeeds.
