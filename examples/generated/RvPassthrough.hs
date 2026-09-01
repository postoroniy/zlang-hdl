{-# LANGUAGE DataKinds #-}
{-# LANGUAGE DeriveAnyClass #-}
{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE NoImplicitPrelude #-}

module RvPassthrough where

import Clash.Prelude
import GHC.Generics (Generic)

data ZLangReadyValidForward a = ZLangReadyValidForward
  { zlangRvPayload :: a
  , zlangRvValid :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

data ZLangReadyValidBackward = ZLangReadyValidBackward
  { zlangRvReady :: Bit
  } deriving (Generic, NFDataX, Show, Eq)

topEntity :: ZLangReadyValidForward (Unsigned 8) -> ZLangReadyValidBackward -> (ZLangReadyValidBackward, ZLangReadyValidForward (Unsigned 8))
topEntity (ZLangReadyValidForward rx_payload rx_valid) (ZLangReadyValidBackward tx_ready) = (ZLangReadyValidBackward rx_ready, ZLangReadyValidForward tx_payload tx_valid)
 where
  tx_payload = rx_payload
  tx_valid = rx_valid
  rx_ready = tx_ready

{-# ANN topEntity
  (Synthesize
    { t_name = "RvPassthrough"
    , t_inputs = [PortProduct "rx" [PortName "payload", PortName "valid"], PortName "tx_ready"]
    , t_output = PortProduct "" [PortName "rx_ready", PortProduct "tx" [PortName "payload", PortName "valid"]]
    }) #-}
