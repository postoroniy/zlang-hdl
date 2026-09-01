{-# LANGUAGE DataKinds #-}
{-# LANGUAGE NoImplicitPrelude #-}

module ShiftMultiply where

import Clash.Prelude

topEntity :: Unsigned 8 -> Unsigned 16
topEntity x = (resize (x) :: Unsigned 16) * (resize ((8 :: Unsigned 8)) :: Unsigned 16)

{-# ANN topEntity
  (Synthesize
    { t_name = "ShiftMultiply"
    , t_inputs = [PortName "x"]
    , t_output = PortName "y"
    }) #-}
