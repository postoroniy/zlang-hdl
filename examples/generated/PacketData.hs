{-# LANGUAGE DataKinds #-}
{-# LANGUAGE DeriveAnyClass #-}
{-# LANGUAGE DeriveGeneric #-}
{-# LANGUAGE NoImplicitPrelude #-}

module PacketData where

import Clash.Prelude
import GHC.Generics (Generic)

data Packet = Packet
  { packet_data :: Unsigned 64
  , packet_last :: Bit
  , packet_vc :: Unsigned 2
  } deriving (Generic, NFDataX, Show, Eq)

topEntity :: Packet -> Unsigned 64
topEntity packet = packet_data (packet)

{-# ANN topEntity
  (Synthesize
    { t_name = "PacketData"
    , t_inputs = [PortProduct "packet" [PortName "data", PortName "last", PortName "vc"]]
    , t_output = PortName "y"
    }) #-}
