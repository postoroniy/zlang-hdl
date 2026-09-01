{-# LANGUAGE DataKinds #-}
{-# LANGUAGE DeriveAnyClass #-}
{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE TemplateHaskell #-}
{-# LANGUAGE NoImplicitPrelude #-}
module PacketFixedArbiter where

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

circuit :: HiddenClockResetEnable ZLangSystem => Signal ZLangSystem (ZLangPacketForward (Unsigned 8)) -> Signal ZLangSystem (ZLangPacketForward (Unsigned 8)) -> Signal ZLangSystem ZLangPacketBackward -> (Signal ZLangSystem ZLangPacketBackward, Signal ZLangSystem ZLangPacketBackward, Signal ZLangSystem (ZLangPacketForward (Unsigned 8)))
circuit high_priority low_priority tx_backward = (ZLangPacketBackward <$> high_priority_ready, ZLangPacketBackward <$> low_priority_ready, ZLangPacketForward <$> tx_payload <*> tx_valid <*> tx_last)
 where
  reset_active = unsafeToActiveHigh hasReset
  high_priority_payload = zlangPacketPayload <$> high_priority
  high_priority_valid = zlangPacketValid <$> high_priority
  high_priority_last = zlangPacketLast <$> high_priority
  low_priority_payload = zlangPacketPayload <$> low_priority
  low_priority_valid = zlangPacketValid <$> low_priority
  low_priority_last = zlangPacketLast <$> low_priority
  tx_ready = zlangPacketReady <$> tx_backward
  candidate = (\valid_0 valid_1 -> if valid_0 == high then 0 else if valid_1 == high then 1 else 0) <$> high_priority_valid <*> low_priority_valid
  grant_active = register low grant_active_next
  grant_owner = register (0 :: Unsigned 1) grant_owner_next
  selected = (\active owner available -> if active == high then owner else available) <$> grant_active <*> grant_owner <*> candidate
  selected_valid_raw = (\selected valid_0 valid_1 -> case selected of { 0 -> valid_0; 1 -> valid_1; _ -> valid_0 }) <$> selected <*> high_priority_valid <*> low_priority_valid
  selected_last = (\selected last_0 last_1 -> case selected of { 0 -> last_0; 1 -> last_1; _ -> last_0 }) <$> selected <*> high_priority_last <*> low_priority_last
  selected_payload = (\selected payload_0 payload_1 -> case selected of { 0 -> payload_0; 1 -> payload_1; _ -> payload_0 }) <$> selected <*> high_priority_payload <*> low_priority_payload
  tx_valid = (\valid resetActive -> if resetActive then low else valid) <$> selected_valid_raw <*> reset_active
  tx_last = selected_last
  tx_payload = selected_payload
  tx_transfer = (\valid ready -> valid .&. ready) <$> tx_valid <*> tx_ready
  grant_complete = (\transferred lastBeat -> transferred .&. lastBeat) <$> tx_transfer <*> tx_last
  grant_active_next = (\active selectedValid complete -> if active == high then if complete == high then low else high else if selectedValid == high && complete == low then high else low) <$> grant_active <*> selected_valid_raw <*> grant_complete
  grant_owner_next = (\active owner chosen selectedValid -> if active == low && selectedValid == high then chosen else owner) <$> grant_active <*> grant_owner <*> selected <*> selected_valid_raw
  high_priority_ready = (\chosen valid ready resetActive -> if not resetActive && chosen == 0 && valid == high then ready else low) <$> selected <*> tx_valid <*> tx_ready <*> reset_active
  low_priority_ready = (\chosen valid ready resetActive -> if not resetActive && chosen == 1 && valid == high then ready else low) <$> selected <*> tx_valid <*> tx_ready <*> reset_active

topEntity :: Clock ZLangSystem -> Reset ZLangSystem -> Signal ZLangSystem (ZLangPacketForward (Unsigned 8)) -> Signal ZLangSystem (ZLangPacketForward (Unsigned 8)) -> Signal ZLangSystem ZLangPacketBackward -> (Signal ZLangSystem ZLangPacketBackward, Signal ZLangSystem ZLangPacketBackward, Signal ZLangSystem (ZLangPacketForward (Unsigned 8)))
topEntity clk rst high_priority low_priority tx_backward = exposeClockResetEnable circuit clk rst enableGen high_priority low_priority tx_backward

{-# ANN topEntity
  (Synthesize
    { t_name = "PacketFixedArbiter"
    , t_inputs = [PortName "clk", PortName "rst", PortProduct "high_priority" [PortName "payload", PortName "valid", PortName "last"], PortProduct "low_priority" [PortName "payload", PortName "valid", PortName "last"], PortName "tx_ready"]
    , t_output = PortProduct "" [PortName "high_priority_ready", PortName "low_priority_ready", PortProduct "tx" [PortName "payload", PortName "valid", PortName "last"]]
    }) #-}
