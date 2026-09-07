{-# LANGUAGE DataKinds #-}
{-# LANGUAGE DeriveAnyClass #-}
{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}
module PacketRoundRobin where

import Clash.Prelude
import GHC.Generics (Generic)

data ZLangPacketForward a = ZLangPacketForward
  { zlangPacketPayload :: a
  , zlangPacketValid :: Bit
  , zlangPacketLast :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

data ZLangPacketBackward = ZLangPacketBackward
  { zlangPacketReady :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

createDomain vSystem{vName="ZLangSystem", vResetKind=Synchronous}

circuit :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem (ZLangPacketForward (Unsigned 8)) -> Signal ZLangSystem (ZLangPacketForward (Unsigned 8)) -> Signal ZLangSystem (ZLangPacketForward (Unsigned 8)) -> Signal ZLangSystem (ZLangPacketForward (Unsigned 8)) -> Signal ZLangSystem ZLangPacketBackward -> (Signal ZLangSystem ZLangPacketBackward, Signal ZLangSystem ZLangPacketBackward, Signal ZLangSystem ZLangPacketBackward, Signal ZLangSystem ZLangPacketBackward, Signal ZLangSystem (ZLangPacketForward (Unsigned 8)))
circuit source_a source_b source_c source_d tx_backward = (ZLangPacketBackward <$> source_a_ready, ZLangPacketBackward <$> source_b_ready, ZLangPacketBackward <$> source_c_ready, ZLangPacketBackward <$> source_d_ready, ZLangPacketForward <$> tx_payload <*> tx_valid <*> tx_last)
 where
  reset_active = unsafeToActiveHigh hasReset
  source_a_payload = zlangPacketPayload <$> source_a
  source_a_valid = zlangPacketValid <$> source_a
  source_a_last = zlangPacketLast <$> source_a
  source_b_payload = zlangPacketPayload <$> source_b
  source_b_valid = zlangPacketValid <$> source_b
  source_b_last = zlangPacketLast <$> source_b
  source_c_payload = zlangPacketPayload <$> source_c
  source_c_valid = zlangPacketValid <$> source_c
  source_c_last = zlangPacketLast <$> source_c
  source_d_payload = zlangPacketPayload <$> source_d
  source_d_valid = zlangPacketValid <$> source_d
  source_d_last = zlangPacketLast <$> source_d
  tx_ready = zlangPacketReady <$> tx_backward
  candidate = (\priority valid_0 valid_1 valid_2 valid_3 -> case priority of { 0 -> if valid_0 == high then 0 else if valid_1 == high then 1 else if valid_2 == high then 2 else if valid_3 == high then 3 else 0; 1 -> if valid_1 == high then 1 else if valid_2 == high then 2 else if valid_3 == high then 3 else if valid_0 == high then 0 else 0; 2 -> if valid_2 == high then 2 else if valid_3 == high then 3 else if valid_0 == high then 0 else if valid_1 == high then 1 else 0; 3 -> if valid_3 == high then 3 else if valid_0 == high then 0 else if valid_1 == high then 1 else if valid_2 == high then 2 else 0; _ -> 0 }) <$> next_priority <*> source_a_valid <*> source_b_valid <*> source_c_valid <*> source_d_valid
  grant_active = register low grant_active_next
  grant_owner = register (0 :: Unsigned 2) grant_owner_next
  selected = (\active owner available -> if active == high then owner else available) <$> grant_active <*> grant_owner <*> candidate
  selected_valid_raw = (\selected valid_0 valid_1 valid_2 valid_3 -> case selected of { 0 -> valid_0; 1 -> valid_1; 2 -> valid_2; 3 -> valid_3; _ -> valid_0 }) <$> selected <*> source_a_valid <*> source_b_valid <*> source_c_valid <*> source_d_valid
  selected_last = (\selected last_0 last_1 last_2 last_3 -> case selected of { 0 -> last_0; 1 -> last_1; 2 -> last_2; 3 -> last_3; _ -> last_0 }) <$> selected <*> source_a_last <*> source_b_last <*> source_c_last <*> source_d_last
  selected_payload = (\selected payload_0 payload_1 payload_2 payload_3 -> case selected of { 0 -> payload_0; 1 -> payload_1; 2 -> payload_2; 3 -> payload_3; _ -> payload_0 }) <$> selected <*> source_a_payload <*> source_b_payload <*> source_c_payload <*> source_d_payload
  tx_valid = (\valid resetActive -> if resetActive then low else valid) <$> selected_valid_raw <*> reset_active
  tx_last = selected_last
  tx_payload = selected_payload
  tx_transfer = (\valid ready -> valid .&. ready) <$> tx_valid <*> tx_ready
  grant_complete = (\transferred lastBeat -> transferred .&. lastBeat) <$> tx_transfer <*> tx_last
  grant_active_next = (\active selectedValid complete -> if active == high then if complete == high then low else high else if selectedValid == high && complete == low then high else low) <$> grant_active <*> selected_valid_raw <*> grant_complete
  grant_owner_next = (\active owner chosen selectedValid -> if active == low && selectedValid == high then chosen else owner) <$> grant_active <*> grant_owner <*> selected <*> selected_valid_raw
  next_priority = register (0 :: Unsigned 2) next_priority_next
  next_priority_next = (\priority selected complete -> if complete == high then if selected == 3 then 0 else selected + 1 else priority) <$> next_priority <*> selected <*> grant_complete
  source_a_ready = (\chosen valid ready resetActive -> if not resetActive && chosen == 0 && valid == high then ready else low) <$> selected <*> tx_valid <*> tx_ready <*> reset_active
  source_b_ready = (\chosen valid ready resetActive -> if not resetActive && chosen == 1 && valid == high then ready else low) <$> selected <*> tx_valid <*> tx_ready <*> reset_active
  source_c_ready = (\chosen valid ready resetActive -> if not resetActive && chosen == 2 && valid == high then ready else low) <$> selected <*> tx_valid <*> tx_ready <*> reset_active
  source_d_ready = (\chosen valid ready resetActive -> if not resetActive && chosen == 3 && valid == high then ready else low) <$> selected <*> tx_valid <*> tx_ready <*> reset_active

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> Signal ZLangSystem (ZLangPacketForward (Unsigned 8)) -> Signal ZLangSystem (ZLangPacketForward (Unsigned 8)) -> Signal ZLangSystem (ZLangPacketForward (Unsigned 8)) -> Signal ZLangSystem (ZLangPacketForward (Unsigned 8)) -> Signal ZLangSystem ZLangPacketBackward -> (Signal ZLangSystem ZLangPacketBackward, Signal ZLangSystem ZLangPacketBackward, Signal ZLangSystem ZLangPacketBackward, Signal ZLangSystem ZLangPacketBackward, Signal ZLangSystem (ZLangPacketForward (Unsigned 8)))
topEntity clk rst source_a source_b source_c source_d tx_backward = exposeClockResetEnable circuit clk rst enableGen source_a source_b source_c source_d tx_backward

{-# ANN topEntity
  (Synthesize
    { t_name = "PacketRoundRobin"
    , t_inputs = [PortName "clk", PortName "rst", PortProduct "source_a" [PortName "payload", PortName "valid", PortName "last"], PortProduct "source_b" [PortName "payload", PortName "valid", PortName "last"], PortProduct "source_c" [PortName "payload", PortName "valid", PortName "last"], PortProduct "source_d" [PortName "payload", PortName "valid", PortName "last"], PortName "tx_ready"]
    , t_output = PortProduct "" [PortName "source_a_ready", PortName "source_b_ready", PortName "source_c_ready", PortName "source_d_ready", PortProduct "tx" [PortName "payload", PortName "valid", PortName "last"]]
    }) #-}
