{-# LANGUAGE DataKinds #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}

module SyncMemory where

import Clash.Prelude

createDomain vSystem{vName="ZLangSystem", vResetKind=Synchronous}

circuit :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem (Unsigned 4) -> Signal ZLangSystem (Bit) -> Signal ZLangSystem (Unsigned 4) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8)
circuit read_address write_enable write_address write_data = read_data
 where
  reset_active = unsafeToActiveHigh hasReset
  table_read_address = read_address
  table_write_enable = write_enable
  table_write_address = write_address
  table_write_data = write_data
  table_cells = register (repeat (0 :: Unsigned 8) :: Vec 16 (Unsigned 8)) table_cells_next
  table_read_value = (\cells readAddress -> if readAddress == 0 then cells !! (0 :: Index 16) else (if readAddress == 1 then cells !! (1 :: Index 16) else (if readAddress == 2 then cells !! (2 :: Index 16) else (if readAddress == 3 then cells !! (3 :: Index 16) else (if readAddress == 4 then cells !! (4 :: Index 16) else (if readAddress == 5 then cells !! (5 :: Index 16) else (if readAddress == 6 then cells !! (6 :: Index 16) else (if readAddress == 7 then cells !! (7 :: Index 16) else (if readAddress == 8 then cells !! (8 :: Index 16) else (if readAddress == 9 then cells !! (9 :: Index 16) else (if readAddress == 10 then cells !! (10 :: Index 16) else (if readAddress == 11 then cells !! (11 :: Index 16) else (if readAddress == 12 then cells !! (12 :: Index 16) else (if readAddress == 13 then cells !! (13 :: Index 16) else (if readAddress == 14 then cells !! (14 :: Index 16) else (cells !! (15 :: Index 16))))))))))))))))) <$> table_cells <*> table_read_address
  table_read_data = register (0 :: Unsigned 8) table_read_value
  table_cells_next = (\cells writeEnable writeAddress writeData -> if writeEnable == high then replace (bitCoerce writeAddress :: Index 16) writeData cells else cells) <$> table_cells <*> table_write_enable <*> table_write_address <*> table_write_data
  read_data = table_read_data

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> Signal ZLangSystem (Unsigned 4) -> Signal ZLangSystem (Bit) -> Signal ZLangSystem (Unsigned 4) -> Signal ZLangSystem (Unsigned 8) -> Signal ZLangSystem (Unsigned 8)
topEntity clk rst read_address write_enable write_address write_data = exposeClockResetEnable circuit clk rst enableGen read_address write_enable write_address write_data

{-# ANN topEntity
  (Synthesize
    { t_name = "SyncMemory"
    , t_inputs = [PortName "clk", PortName "rst", PortName "read_address", PortName "write_enable", PortName "write_address", PortName "write_data"]
    , t_output = PortName "read_data"
    }) #-}
