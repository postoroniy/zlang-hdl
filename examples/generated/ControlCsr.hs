{-# LANGUAGE DataKinds #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}

module ControlCsr where

import Clash.Prelude

createDomain vSystem{vName="ZLangSystem", vResetKind=Synchronous}

circuit :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem (Unsigned 32) -> Signal ZLangSystem Bit -> Signal ZLangSystem (BitVector 32) -> Signal ZLangSystem Bit -> (Signal ZLangSystem (BitVector 32), Signal ZLangSystem Bit)
circuit addr write wdata read = (rdata, ready)
 where
  csr_control_control_write_hit = (\address writeRequest -> writeRequest == high && address == (1073741824 :: Unsigned 32)) <$> addr <*> write
  csr_control_control_enable_write_value = slice d0 d0 <$> wdata
  csr_control_control_enable = register (0 :: BitVector 1) csr_control_control_enable_next
  csr_control_control_enable_next = (\old writeHit incoming -> if writeHit then incoming else old) <$> csr_control_control_enable <*> csr_control_control_write_hit <*> csr_control_control_enable_write_value
  csr_control_control_mode_write_value = slice d3 d1 <$> wdata
  csr_control_control_mode = register (0 :: BitVector 3) csr_control_control_mode_next
  csr_control_control_mode_next = (\old writeHit incoming -> if writeHit then incoming else old) <$> csr_control_control_mode <*> csr_control_control_write_hit <*> csr_control_control_mode_write_value
  csr_control_control_start_write_value = slice d4 d4 <$> wdata
  csr_control_control_start = register (0 :: BitVector 1) csr_control_control_start_next
  csr_control_control_start_next = (\_ writeHit incoming -> if writeHit then incoming else 0) <$> csr_control_control_start <*> csr_control_control_write_hit <*> csr_control_control_start_write_value
  csr_control_control_command_write_value = slice d7 d5 <$> wdata
  csr_control_control_command = register (0 :: BitVector 3) csr_control_control_command_next
  csr_control_control_command_next = (\old writeHit incoming -> if writeHit then incoming else old) <$> csr_control_control_command <*> csr_control_control_write_hit <*> csr_control_control_command_write_value
  csr_control_control_read_word = (\field0 field1 -> shiftL (resize field0 :: BitVector 32) 0 .|. shiftL (resize field1 :: BitVector 32) 1) <$> csr_control_control_enable <*> csr_control_control_mode
  csr_control_status_write_hit = (\address writeRequest -> writeRequest == high && address == (1073741828 :: Unsigned 32)) <$> addr <*> write
  csr_control_status_busy = pure (1 :: BitVector 1)
  csr_control_status_error_write_value = slice d1 d1 <$> wdata
  csr_control_status_error = register (1 :: BitVector 1) csr_control_status_error_next
  csr_control_status_error_next = (\old writeHit incoming -> if writeHit then old .&. complement incoming else old) <$> csr_control_status_error <*> csr_control_status_write_hit <*> csr_control_status_error_write_value
  csr_control_status_read_word = (\field0 field1 -> shiftL (resize field0 :: BitVector 32) 0 .|. shiftL (resize field1 :: BitVector 32) 1) <$> csr_control_status_busy <*> csr_control_status_error
  rdata = (\address readRequest word0 word1 -> if readRequest == low then 0 else case address of { 1073741824 -> word0; 1073741828 -> word1; _ -> 0 }) <$> addr <*> read <*> csr_control_control_read_word <*> csr_control_status_read_word
  ready = (\address readRequest writeRequest -> if readRequest == high || writeRequest == high then case address of { 1073741824 -> high; 1073741828 -> high; _ -> low } else low) <$> addr <*> read <*> write

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> Signal ZLangSystem (Unsigned 32) -> Signal ZLangSystem Bit -> Signal ZLangSystem (BitVector 32) -> Signal ZLangSystem Bit -> (Signal ZLangSystem (BitVector 32), Signal ZLangSystem Bit)
topEntity clk rst addr write wdata read = exposeClockResetEnable circuit clk rst enableGen addr write wdata read

{-# ANN topEntity
  (Synthesize
    { t_name = "ControlCsr"
    , t_inputs = [PortName "clk", PortName "rst", PortName "addr", PortName "write", PortName "wdata", PortName "read"]
    , t_output = PortProduct "" [PortName "rdata", PortName "ready"]
    }) #-}
