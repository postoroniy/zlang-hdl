# Third-party runtime licenses

The reviewed native workspace uses the exact crates below from the locked
crates.io index. This conservative inventory covers the debugger, waveform
indexer and companion native-simulation wheel; an individual executable may
link only a subset. It is generated from `Cargo.lock` plus offline Cargo
metadata, and the private release audit rejects Git/path dependencies, missing
checksums, missing licenses and license identifiers outside the reviewed
allow-list.

| Crate | Version | SPDX license expression | Cargo checksum |
|---|---:|---|---|
| `adler2` | `2.0.1` | `0BSD OR MIT OR Apache-2.0` | `320119579fcad9c21884f5c4861d16174d0e06250625266f50fe6898340abefa` |
| `allocator-api2` | `0.2.21` | `MIT OR Apache-2.0` | `683d7910e743518b0e34f1186f92494becacb047c7b6bf616c96772180fef923` |
| `anyhow` | `1.0.104` | `MIT OR Apache-2.0` | `330a5ed07fa54e4702c9d6c4174f74427fc0ef6e214bbd677ae50a5099946470` |
| `arbitrary` | `1.4.2` | `MIT OR Apache-2.0` | `c3d036a3c4ab069c7b410a2ce876bd74808d2d0888a82667669f8e783a898bf1` |
| `bitflags` | `1.3.2` | `MIT/Apache-2.0` | `bef38d45163c2f1dde094a7dfd33ccf595c92905c8f8f4fdc18d06fb1037718a` |
| `block-buffer` | `0.10.4` | `MIT OR Apache-2.0` | `3078c7629b62d3f0439517fa394996acacc5cbc91c5a20d8c658e77abd503a71` |
| `bumpalo` | `3.20.3` | `MIT OR Apache-2.0` | `72f5acc6cb2ba439de613abc23857ec3d78374d8ed5ac84e9d11336e87da8649` |
| `cfg-if` | `1.0.5` | `MIT OR Apache-2.0` | `4e7648175b45a9a48536d676f68d918270699102aa8dab5496df06904c914600` |
| `cpufeatures` | `0.2.17` | `MIT OR Apache-2.0` | `59ed5838eebb26a2bb2e58f6d5b5316989ae9d08bab10e0e6d103e656d1b0280` |
| `cranelift-assembler-x64` | `0.135.2` | `Apache-2.0 WITH LLVM-exception` | `42c0ca95f115597232c3c5d7286bf847d49332bee5934749f40808b7fe0c6312` |
| `cranelift-assembler-x64-meta` | `0.135.2` | `Apache-2.0 WITH LLVM-exception` | `fdd7d59ef8a52c82ea2ac2ca96aee3c52644845440f70611b573bc20ce37e6a2` |
| `cranelift-bforest` | `0.135.2` | `Apache-2.0 WITH LLVM-exception` | `547433a3f4204f0ed0f5a5e3ffaf086a0c005fd0efdf040098083f38572e8a49` |
| `cranelift-bitset` | `0.135.2` | `Apache-2.0 WITH LLVM-exception` | `61ca4f30f5f2eb3837b6e73664d9c0c9a42c4c0dce05f0027f2f2af54c1b20d7` |
| `cranelift-codegen` | `0.135.2` | `Apache-2.0 WITH LLVM-exception` | `effcb11ccea65cfb2b8dd4f53b92c6449ad37745504bae036bffda597a5cfa48` |
| `cranelift-codegen-meta` | `0.135.2` | `Apache-2.0 WITH LLVM-exception` | `2cd5b4e429e24f7fd0bfe15d653ec5841fc1831ef68521b0d30182ef0d48bd01` |
| `cranelift-codegen-shared` | `0.135.2` | `Apache-2.0 WITH LLVM-exception` | `a8aefe7ed05aa9a831a4696f55e020a6c93998325a09c642863f0be7845fe3a8` |
| `cranelift-control` | `0.135.2` | `Apache-2.0 WITH LLVM-exception` | `b3ceaa06b6ece027bb001093bf0c6984a96281e061fd40a99c60bae0ec57171d` |
| `cranelift-entity` | `0.135.2` | `Apache-2.0 WITH LLVM-exception` | `bc2c3088c6be94765abea42eccff7ac2b6519488e66bb5159febabcf539afd6e` |
| `cranelift-frontend` | `0.135.2` | `Apache-2.0 WITH LLVM-exception` | `0e2c4a0a90aedfa88408f33e65eda7b1f73de0af20ebec516c004b3a1cdc602b` |
| `cranelift-isle` | `0.135.2` | `Apache-2.0 WITH LLVM-exception` | `bcb1cc85ccb60cfc53c3d55e23196fef28ae78fbcebe4e1ff3ff866a3c93d61c` |
| `cranelift-jit` | `0.135.2` | `Apache-2.0 WITH LLVM-exception` | `2cd6eefdafb491a0adca958990ce5374e5e5597a7e02c495f0575da52e6813a0` |
| `cranelift-module` | `0.135.2` | `Apache-2.0 WITH LLVM-exception` | `b6d356ef498fa5f064fc64ae3c1a6b22289e24b8ef0769604fd410bebe0aebf7` |
| `cranelift-native` | `0.135.2` | `Apache-2.0 WITH LLVM-exception` | `5a0a8f5d008c1f5b78426e83028f7517295f6819b5cebe8193949e1bb66cc01a` |
| `cranelift-srcgen` | `0.135.2` | `Apache-2.0 WITH LLVM-exception` | `56caa8a7f1e0f2c61400996cc1567fb06dd417c49d5e673131a149cb9628625f` |
| `crossbeam-deque` | `0.8.8` | `MIT OR Apache-2.0` | `622f3fc73690be383c7214310406f28a90e6edeadc3cea882f9d71e495b9711a` |
| `crossbeam-epoch` | `0.9.21` | `MIT OR Apache-2.0` | `dc74980687109a3b14c72fd458107bf0baa1da1a1a805e178d15501ba9b86d9d` |
| `crossbeam-utils` | `0.8.23` | `MIT OR Apache-2.0` | `a31eee39dddec8330830986fcd7625edb5a24ec90ea038215273bbc3adb08ac6` |
| `crypto-common` | `0.1.7` | `MIT OR Apache-2.0` | `78c8292055d1c1df0cce5d180393dc8cce0abec0a7102adb6c7b1eef6016d60a` |
| `digest` | `0.10.7` | `MIT OR Apache-2.0` | `9ed9a281f7bc9b7576e61468ba615a66a5c8cfdff42420a70aa82701a3b1e292` |
| `either` | `1.19.0` | `MIT OR Apache-2.0` | `0e9c71c2167ca323c882b99918929403426e2373ea17242ff5653e0d5e1058be` |
| `equivalent` | `1.0.2` | `Apache-2.0 OR MIT` | `877a4ace8713b0bcf2a4e7eec82529c029f1d0619886d18145fea96c3ffe5c0f` |
| `fnv` | `1.0.7` | `Apache-2.0 / MIT` | `3f9eec918d3f24069decb9af1554cad7c880e2da24a9afd88aca000531ab82c1` |
| `foldhash` | `0.2.0` | `Zlib` | `77ce24cb58228fbb8aa041425bb1050850ac19177686ea6e0f41a70416f56fdb` |
| `fst-reader` | `0.17.1` | `BSD-3-Clause` | `a9875223756618df05c8e7aae5e6a704fab17940337eb5b2fb962ed03853b4ce` |
| `fst-writer` | `0.3.1` | `BSD-3-Clause` | `77be37fdc968866fa4f0cee73e42185e6d4699a761a51a54e6ebc9a3d73ed537` |
| `generic-array` | `0.14.7` | `MIT` | `85649ca51fd72272d7821adaf274ad91c288277713d9c18820d8499a7ff69e9a` |
| `gimli` | `0.33.0` | `MIT OR Apache-2.0` | `0bf7f043f89559805f8c7cacc432749b2fa0d0a0a9ee46ce47164ed5ba7f126c` |
| `hashbrown` | `0.16.1` | `MIT OR Apache-2.0` | `841d1cc9bed7f9236f321df977030373f4a4163ae1a7dbfe1a51a2c1a51d9100` |
| `hashbrown` | `0.17.1` | `MIT OR Apache-2.0` | `ed5909b6e89a2db4456e54cd5f673791d7eca6732202bbf2a9cc504fe2f9b84a` |
| `heck` | `0.5.0` | `MIT OR Apache-2.0` | `2304e00983f87ffb38b55b444b5e3b60a884b5d30c0fca7d82fe33449bbe55ea` |
| `indexmap` | `2.14.2` | `Apache-2.0 OR MIT` | `cc4e190f5d26ca7051642629da2c52fc03bde85a03197c99408dcd291734c855` |
| `itoa` | `1.0.18` | `MIT OR Apache-2.0` | `8f42a60cbdf9a97f5d2305f08a87dc4e09308d1276d28c869c684d7777685682` |
| `leb128` | `0.2.7` | `MIT OR Apache-2.0` | `c83bff1d572d6b9aeef67ddfc8448e4a3737909cb28e81f97c791b9018703e52` |
| `libc` | `0.2.189` | `MIT OR Apache-2.0` | `3eaf3ede3fee6db1a4c2ee091bf8a8b4dccdc6d17f656fb07896ee72867612f2` |
| `libm` | `0.2.16` | `MIT` | `b6d2cec3eae94f9f509c767b45932f1ada8350c4bdb85af2fcab4a3c14807981` |
| `log` | `0.4.34` | `MIT OR Apache-2.0` | `f9f8bd3e56ce4dfc153cf470fffbfa98c7620958b312ca5c3a4b8d5181fd13c6` |
| `lz4_flex` | `0.11.6` | `MIT` | `373f5eceeeab7925e0c1098212f2fbc4d416adec9d35051a6ab251e824c1854a` |
| `lz4_flex` | `0.13.1` | `MIT` | `7ef0d4ed8669f8f8826eb00dc878084aa8f253506c4fd5e8f58f5bce72ddb97e` |
| `mach2` | `0.4.3` | `BSD-2-Clause OR MIT OR Apache-2.0` | `d640282b302c0bb0a2a8e0233ead9035e3bed871f0b7e81fe4a1ec829765db44` |
| `memchr` | `2.8.3` | `Unlicense OR MIT` | `cf8baf1c55e62ffcace7a9f06f4bd9cd3f0c4beb022d3b367256b91b87513d98` |
| `memmap2` | `0.9.11` | `MIT OR Apache-2.0` | `d1219ed1b7f229ee7104d281dd01d6802fe28bb6e95d292942c4daacdeb798c0` |
| `miniz_oxide` | `0.8.9` | `MIT OR Zlib OR Apache-2.0` | `1fa76a2c86f704bdb222d66965fb3d63269ce38518b83cb0575fca855ebb6316` |
| `miniz_oxide` | `0.9.1` | `MIT OR Zlib OR Apache-2.0` | `b63fbc4a50860e98e7b2aa7804ded1db5cbc3aff9193adaff57a6931bf7c4b4c` |
| `num_enum` | `0.7.6` | `BSD-3-Clause OR MIT OR Apache-2.0` | `5d0bca838442ec211fa11de3a8b0e0e8f3a4522575b5c4c06ed722e005036f26` |
| `num_enum_derive` | `0.7.6` | `BSD-3-Clause OR MIT OR Apache-2.0` | `680998035259dcfcafe653688bf2aa6d3e2dc05e98be6ab46afb089dc84f1df8` |
| `once_cell` | `1.21.4` | `MIT OR Apache-2.0` | `9f7c3e4beb33f85d45ae3e3a1792185706c8e16d043238c593331cc7cd313b50` |
| `portable-atomic` | `1.15.0` | `Apache-2.0 OR MIT` | `05c8b63e8d9609db387f0324918f81d68fe27748f084ef092fb35954d0539a85` |
| `proc-macro-crate` | `3.5.0` | `MIT OR Apache-2.0` | `e67ba7e9b2b56446f1d419b1d807906278ffa1a658a8a5d8a39dcb1f5a78614f` |
| `proc-macro2` | `1.0.107` | `MIT OR Apache-2.0` | `985e7ec9bb745e6ce6535b544d84d6cd6f7ad8bd711c398938ae983b91a766d9` |
| `pyo3` | `0.29.2` | `MIT OR Apache-2.0` | `4688ddedf473e32662b9b067670129a8afb8c18e351482c70d62ba4a88171e8b` |
| `pyo3-build-config` | `0.29.2` | `MIT OR Apache-2.0` | `f41027e41b4bd03f6e60f9f417fe24a6341a6bb744edd62b6f709f2a52ea30e9` |
| `pyo3-ffi` | `0.29.2` | `MIT OR Apache-2.0` | `e591a95526fead067432c3b3a33fc74770b87b1e04e73671090d9c2055a2b327` |
| `pyo3-macros` | `0.29.2` | `MIT OR Apache-2.0` | `73225868fc1cd84eef2c3c230ddb91273bf1de46aeb8a4248da76d32a0924a1c` |
| `pyo3-macros-backend` | `0.29.2` | `MIT OR Apache-2.0` | `571575aa3749fa6216757dd47d2a3e7ef360f329a40f0666a9fbd14889024952` |
| `quote` | `1.0.47` | `MIT OR Apache-2.0` | `1fbf4db142a473a8d80c26bbf18454ed458bf8d26c8219c331daecfdbd079001` |
| `rayon` | `1.12.0` | `MIT OR Apache-2.0` | `fb39b166781f92d482534ef4b4b1b2568f42613b53e5b6c160e24cfbfa30926d` |
| `rayon-core` | `1.13.0` | `MIT OR Apache-2.0` | `22e18b0f0062d30d4230b2e85ff77fdfe4326feb054b9783a3460d8435c8ab91` |
| `regalloc2` | `0.15.2` | `Apache-2.0 WITH LLVM-exception` | `757712e8e61590d6d4f5d563483755538b5aa13467837a3b41cd9832509a7f85` |
| `region` | `3.0.2` | `MIT` | `e6b6ebd13bc009aef9cd476c1310d49ac354d36e240cf1bd753290f3dc7199a7` |
| `rustc-hash` | `2.1.3` | `Apache-2.0 OR MIT` | `6b1e7f9a428571be2dc5bc0505c13fb6bf936822b894ec87abf8a08a4e51742d` |
| `rustversion` | `1.0.23` | `MIT OR Apache-2.0` | `cf54715a573b99ac80df0bc206da022bcd442c974952c7b9720069370852e21f` |
| `serde` | `1.0.228` | `MIT OR Apache-2.0` | `9a8e94ea7f378bd32cbbd37198a4a91436180c5bb472411e48b5ec2e2124ae9e` |
| `serde_core` | `1.0.228` | `MIT OR Apache-2.0` | `41d385c7d4ca58e59fc732af25c3983b67ac852c1a25000afe1175de458b67ad` |
| `serde_derive` | `1.0.228` | `MIT OR Apache-2.0` | `d540f220d3187173da220f885ab66608367b6574e925011a9353e4badda91d79` |
| `serde_json` | `1.0.149` | `MIT OR Apache-2.0` | `83fc039473c5595ace860d8c4fafa220ff474b3fc6bfdb4293327f1a37e94d86` |
| `sha2` | `0.10.9` | `MIT OR Apache-2.0` | `a7507d819769d01a365ab707794a4084392c824f54a7a6a7862f8c3d0892b283` |
| `smallvec` | `1.16.1` | `MIT OR Apache-2.0` | `ba467056f1b547ed52077911161fc86985becbc60e8e1857c8a144dab0def891` |
| `stable_deref_trait` | `1.2.1` | `MIT OR Apache-2.0` | `6ce2be8dc25455e1f91df71bfa12ad37d7af1092ae736f3a6cd0e37bc7810596` |
| `syn` | `2.0.119` | `MIT OR Apache-2.0` | `872831b642d1a07999a962a351ed35b955ea2cfc8f3862091e2a240a84f17297` |
| `syn` | `3.0.9` | `MIT OR Apache-2.0` | `d78c8dee4c7bf0e14673097256fed6142ce9d3b85a408189d07482442145823b` |
| `target-lexicon` | `0.13.5` | `Apache-2.0 WITH LLVM-exception` | `adb6935a6f5c20170eeceb1a3835a49e12e19d792f6dd344ccc76a985ca5a6ca` |
| `thiserror` | `2.0.21` | `MIT OR Apache-2.0` | `09e52cb86a36cede5cb101bf8908837b3e4c6e5e59fe7fd85c23fb56200d189e` |
| `thiserror-impl` | `2.0.21` | `MIT OR Apache-2.0` | `fe5197923287db20a58125f0bc85c062f7f2c892de97b18c356f9efb14b28524` |
| `toml_datetime` | `1.1.2+spec-1.1.0` | `MIT OR Apache-2.0` | `2b86d767906c6c42421dcba507eb9d203e779497710a47782a224bb871653053` |
| `toml_edit` | `0.25.17+spec-1.1.0` | `MIT OR Apache-2.0` | `e3641d5bbb5349a79e1020a242d251efbc546ad8048d133958323ce9c40a9c9c` |
| `toml_parser` | `1.1.5+spec-1.1.0` | `MIT OR Apache-2.0` | `baa693a8032d7e1cada7d0041e96126df243179ff061456783ac7f12bda4744c` |
| `twox-hash` | `2.1.5` | `MIT` | `86a801b3cea342a06d468c8710662aa29e5e05e4f5c0d62f00bbb7f2ad7941c2` |
| `typenum` | `1.20.1` | `MIT OR Apache-2.0` | `b6f5e870be6c3b371b77fe0ee0bafb859fa4964b4404c27de1d380043c4dda20` |
| `unicode-ident` | `1.0.26` | `(MIT OR Apache-2.0) AND Unicode-3.0` | `d245f478577f809a851594d02313b640fb437e0bb33866753cff937863096954` |
| `version_check` | `0.9.5` | `MIT/Apache-2.0` | `0b928f33d975fc6ad9f86c8f283853ad26bdd5b10b7f1542aa2fa15e2289105a` |
| `wasmtime-internal-core` | `48.0.2` | `Apache-2.0 WITH LLVM-exception` | `8679b612a408274d91d1e0edf6baa36b63cc48d937f84dd1e7fb2fbe16a907ae` |
| `wasmtime-internal-jit-icache-coherence` | `48.0.2` | `Apache-2.0 WITH LLVM-exception` | `bf7d8a6fc6d21eb177c38f096636661ca13ae924cccbb6f447c1498ca972ab10` |
| `wellen` | `0.25.6` | `BSD-3-Clause` | `5b415caa0ba4453072996e8e153d46f66aa52ebd314ebf1423d52d3bf3f85ef8` |
| `windows-link` | `0.2.1` | `MIT OR Apache-2.0` | `f0805222e57f7521d6a62e36fa9163bc891acd422f971defe97d64e70d0a4fe5` |
| `windows-sys` | `0.52.0` | `MIT OR Apache-2.0` | `282be5f36a8ce781fad8c8ae18fa3f9beff57ec1b52cb3de0789201425d9a33d` |
| `windows-sys` | `0.61.2` | `MIT OR Apache-2.0` | `ae137229bcbd6cdf0f7b80a31df61766145077ddf49416a728b02cb3921ff3fc` |
| `windows-targets` | `0.52.6` | `MIT OR Apache-2.0` | `9b724f72796e036ab90c1021d4780d4d3d648aca59e491e6b98e725b84e99973` |
| `windows_aarch64_gnullvm` | `0.52.6` | `MIT OR Apache-2.0` | `32a4622180e7a0ec044bb555404c800bc9fd9ec262ec147edd5989ccd0c02cd3` |
| `windows_aarch64_msvc` | `0.52.6` | `MIT OR Apache-2.0` | `09ec2a7bb152e2252b53fa7803150007879548bc709c039df7627cabbd05d469` |
| `windows_i686_gnu` | `0.52.6` | `MIT OR Apache-2.0` | `8e9b5ad5ab802e97eb8e295ac6720e509ee4c243f69d781394014ebfe8bbfa0b` |
| `windows_i686_gnullvm` | `0.52.6` | `MIT OR Apache-2.0` | `0eee52d38c090b3caa76c563b86c3a4bd71ef1a819287c19d586d7334ae8ed66` |
| `windows_i686_msvc` | `0.52.6` | `MIT OR Apache-2.0` | `240948bc05c5e7c6dabba28bf89d89ffce3e303022809e73deaefe4f6ec56c66` |
| `windows_x86_64_gnu` | `0.52.6` | `MIT OR Apache-2.0` | `147a5c80aabfbf0c7d901cb5895d1de30ef2907eb21fbbab29ca94c5b08b1a78` |
| `windows_x86_64_gnullvm` | `0.52.6` | `MIT OR Apache-2.0` | `24d5b23dc417412679681396f2b49f3de8c1473deb516bd34410872eff51ed0d` |
| `windows_x86_64_msvc` | `0.52.6` | `MIT OR Apache-2.0` | `589f6da84c646204747d1270a2a5661ea66ed1cced2631d546fdfb155959f9ec` |
| `winnow` | `1.0.4` | `MIT` | `23b97319f7b8343df12cc98938e5c3eb436064524c8d2b4e30a1d3a36eecdf81` |
| `zmij` | `1.0.23` | `MIT` | `29666d0abbfad1e3dc4dcf6144730dd3a3ab225bbbdac83319345b1b44ccfc1b` |

The companion wheel contains a CycloneDX SBOM generated by the pinned Maturin
build. Its component versions and license expressions must match this
workspace inventory exactly. ZLang HDL and the native executables remain
licensed under Apache-2.0; third-party components retain their own terms.
