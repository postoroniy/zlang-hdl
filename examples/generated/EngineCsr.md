# EngineCsr CSR map

Bus width: 32 bits.

## engine

Base address: `0x50000000`

### CONTROL

Offset `0x00`, address `0x50000000`.

| Field | Bits | Type | Access | Reset | Hardware | Priority |
|---|---:|---|---|---:|---|---|
| start | 0 | `bit` | `pulse` | `0x0` | `command:engine_start` | — |
| reserved0 | 31:1 | `bits<31>` | `reserved` | `0x0` | — | — |

### STATUS

Offset `0x04`, address `0x50000004`.

| Field | Bits | Type | Access | Reset | Hardware | Priority |
|---|---:|---|---|---:|---|---|
| busy | 0 | `bit` | `ro` | `0x0` | `status:engine_busy` | — |
| error | 1 | `bit` | `w1c` | `0x0` | `sticky:engine_error` | `hardware` |
| reserved1 | 31:2 | `bits<30>` | `reserved` | `0x0` | — | — |
