<a id="reference-storage-memories"></a>
## Storage and memories

Storage declarations state logical behavior. A target mapping is legal only
when it preserves the declared ports, latency, collision, mask, reset, and
initialization contract.

### FIFO

```zlang
fifo queue : fifo<u8,4>
queue.data = rx.payload
queue.push = rx.transfer
queue.pop = tx.transfer
```

Read-only observations include `.front`, `.count`, `.full`, `.empty`,
`.ready`, `.valid`, `.overflow`, and `.underflow`. A legal simultaneous
pop/push while full preserves occupancy and ordering. An empty pop cannot
consume a same-cycle push. Rules may instead own `queue.push(value)` and
`queue.pop()` atomically; rule actions and globally driven controls cannot be
mixed for one FIFO.

### Synchronous memory

```zlang
memory table : mem<u32,256> {
    read_latency 1
    collision write_first
}
table.read_address = read_address
table.write_enable = write_enable
table.write_address = write_address
table.write_data = write_data
read_data = table.read_data
```

An ordinary `mem` accepts `read_latency 0..16`: zero is combinational and each
positive value is an exact number of read-domain edges from address capture to
the visible result. Rule-owned memory reads have latency one and hold their
result when no read fires. A target's synchronous RAM route may support a
narrower subset; preferred policy can retain generic RTL, while required policy
rejects a mismatch.

`collision old`, `new`, and `no_change` define a same-clock registered-read
collision. `read_first` and `write_first` are aliases for `old` and `new`.
Reset suppresses writes and actions.

### Byte masks

An optional mask is exactly `bits<ceil(element_width / 8)>`.
`write_mask[0]` controls the least-significant byte. The final lane contains
only live bits when the element width is not byte-aligned; disabled lanes retain
their prior values. A `write_first` read returns the post-mask merged word.

```zlang
table.write_mask = write_mask
rule store when enable { table.write(address, data, byte_mask) }
```

### Named same-clock ports

Up to eight named `read_port`, `write_port`, or `read_write_port` declarations
may share one resolved clock domain:

```zlang
memory table : mem<u32,1024> @clk {
    read_write_port a
    read_write_port b
    read_latency 1
    collision old
    write_priority a > b
}
```

Every writer appears exactly once in `write_priority`. Priority applies only to
writes to the same address. Mixed port widths and asymmetric depths are errors.
A matching target resource may implement the shape; bounded 1W+nR replication
and small register-array fallback are available where documented. Banking and
arbitration remain explicit source architecture.

### Reset and initialization

`init VALUE` supplies one exact compile-time element value for every cell.
With `contents clear`, reset writes the declared value in generic/register-array
hardware. With `contents preserve`, it is a deterministic power-up value and
runtime reset leaves cells unchanged. The optional reset block must specify
both contents and read-data policy:

```zlang
memory table : mem<u32,256> {
    read_latency 1
    collision old
    reset {
        contents preserve
        read_data preserve
    }
}
```

Without the block, both policies are `clear`. A target binding must advertise
compatible initialization and reset behavior; inference in one tool run is not
portable evidence.

### Explicit dual-clock memory

`async_mem<T,N>` has one write-only port and one read-only port in different
explicit domains:

```zlang
memory table : async_mem<u32,1024> {
    write_port wr @write_clk
    read_port rd @read_clk
    read_latency 1
    collision old
    reset { contents preserve read_data clear }
}
```

Cells change on the writer edge; visible read data changes on the reader edge.
The accepted read latency is `1..16`. Coincident logical edges use one pre-edge
snapshot: `old` observes the previous cell and `new` forwards the coincident
write. This is a digital contract, not a metastability guarantee. Stronger
cross-clock collision claims require exact target evidence.

### ROM

```zlang
rom twiddles : rom<Complex<Twiddle>, N / 2> {
    read_latency 1
    init fft_twiddles<T=Twiddle,N=N>()
}
```

A ROM initializer evaluates at compile time to exactly
`vec<depth,element_type>`. Reads are synchronous with one-cycle latency; reset
clears the result register but never changes immutable contents. Direct-SV
emits a deterministic companion image with address zero first and the canonical
packed layout described in Chapter 5.

### Mapping boundary

Memory elements may be recursively bit-packable non-enum aggregates as well as
scalars. Full or partial writable images, automatic banking, unbounded
multiport storage, mixed-width ports, ECC, and asynchronous 2RW memory are not
part of the current language surface. Standard-library wrappers compose common
shapes through these same memory contracts; their names do not grant special
compiler semantics.
