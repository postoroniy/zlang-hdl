{-# LANGUAGE DataKinds #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}

module EngineCsr where

import Clash.Prelude

createDomain vSystem{vName="ZLangSystem", vResetKind=Synchronous}

circuit :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem (Unsigned 32) -> Signal ZLangSystem Bit -> Signal ZLangSystem (BitVector 32) -> Signal ZLangSystem Bit -> Signal ZLangSystem (Bit) -> Signal ZLangSystem (Bit) -> (Signal ZLangSystem (BitVector 32), Signal ZLangSystem Bit, Signal ZLangSystem (Bit))
circuit addr write wdata read engine_busy engine_error = (rdata, ready, engine_start)
 where
  csr_engine_control_write_hit = (\address writeRequest -> writeRequest == high && address == (1342177280 :: Unsigned 32)) <$> addr <*> write
  csr_engine_control_start_write_value = slice d0 d0 <$> wdata
  csr_engine_control_start = register (0 :: BitVector 1) csr_engine_control_start_next
  csr_engine_control_start_next = (\_ writeHit incoming -> if writeHit then incoming else 0) <$> csr_engine_control_start <*> csr_engine_control_write_hit <*> csr_engine_control_start_write_value
  engine_start = unpack <$> csr_engine_control_start
  csr_engine_control_read_word = pure (0 :: BitVector 32)
  csr_engine_status_write_hit = (\address writeRequest -> writeRequest == high && address == (1342177284 :: Unsigned 32)) <$> addr <*> write
  csr_engine_status_busy = pack <$> engine_busy
  csr_engine_status_error_write_value = slice d1 d1 <$> wdata
  csr_engine_status_error = register (0 :: BitVector 1) csr_engine_status_error_next
  csr_engine_status_error_hardware_set = pack <$> engine_error
  csr_engine_status_error_next = (\old writeHit incoming hardwareSet -> (old .&. complement (if writeHit then incoming else 0)) .|. hardwareSet) <$> csr_engine_status_error <*> csr_engine_status_write_hit <*> csr_engine_status_error_write_value <*> csr_engine_status_error_hardware_set
  csr_engine_status_read_word = (\field0 field1 -> shiftL (resize field0 :: BitVector 32) 0 .|. shiftL (resize field1 :: BitVector 32) 1) <$> csr_engine_status_busy <*> csr_engine_status_error
  rdata = (\address readRequest word0 word1 -> if readRequest == low then 0 else case address of { 1342177280 -> word0; 1342177284 -> word1; _ -> 0 }) <$> addr <*> read <*> csr_engine_control_read_word <*> csr_engine_status_read_word
  ready = (\address readRequest writeRequest -> if readRequest == high || writeRequest == high then case address of { 1342177280 -> high; 1342177284 -> high; _ -> low } else low) <$> addr <*> read <*> write

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> Signal ZLangSystem (Unsigned 32) -> Signal ZLangSystem Bit -> Signal ZLangSystem (BitVector 32) -> Signal ZLangSystem Bit -> Signal ZLangSystem (Bit) -> Signal ZLangSystem (Bit) -> (Signal ZLangSystem (BitVector 32), Signal ZLangSystem Bit, Signal ZLangSystem (Bit))
topEntity clk rst addr write wdata read engine_busy engine_error = exposeClockResetEnable circuit clk rst enableGen addr write wdata read engine_busy engine_error

{-# ANN topEntity
  (Synthesize
    { t_name = "EngineCsr"
    , t_inputs = [PortName "clk", PortName "rst", PortName "addr", PortName "write", PortName "wdata", PortName "read", PortName "engine_busy", PortName "engine_error"]
    , t_output = PortProduct "" [PortName "rdata", PortName "ready", PortName "engine_start"]
    }) #-}
