{-# LANGUAGE DataKinds #-}
{-# LANGUAGE NoImplicitPrelude #-}

module FIR2 where

import Clash.Prelude

tap :: Unsigned 8 -> Unsigned 8 -> Unsigned 16
tap sample coefficient = (resize (sample) :: Unsigned 16) * (resize (coefficient) :: Unsigned 16)

topEntity :: Vec 2 (Unsigned 8) -> Vec 2 (Unsigned 8) -> Unsigned 17
topEntity samples coefficients = (resize (tap ((samples) !! (0 :: Index 2)) ((coefficients) !! (0 :: Index 2))) :: Unsigned 17) + (resize (tap ((samples) !! (1 :: Index 2)) ((coefficients) !! (1 :: Index 2))) :: Unsigned 17)

{-# ANN topEntity
  (Synthesize
    { t_name = "FIR2"
    , t_inputs = [PortName "samples", PortName "coefficients"]
    , t_output = PortName "y"
    }) #-}
