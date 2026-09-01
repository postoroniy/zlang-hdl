{-# LANGUAGE DataKinds #-}
{-# LANGUAGE NoImplicitPrelude #-}

module DotBuiltin where

import Clash.Prelude

topEntity :: Vec 8 (Unsigned 8) -> Vec 8 (Unsigned 8) -> Unsigned 19
topEntity a b = (resize ((resize ((resize ((resize ((a) !! (0 :: Index 8)) :: Unsigned 16) * (resize ((b) !! (0 :: Index 8)) :: Unsigned 16)) :: Unsigned 17) + (resize ((resize ((a) !! (1 :: Index 8)) :: Unsigned 16) * (resize ((b) !! (1 :: Index 8)) :: Unsigned 16)) :: Unsigned 17)) :: Unsigned 18) + (resize ((resize ((resize ((a) !! (2 :: Index 8)) :: Unsigned 16) * (resize ((b) !! (2 :: Index 8)) :: Unsigned 16)) :: Unsigned 17) + (resize ((resize ((a) !! (3 :: Index 8)) :: Unsigned 16) * (resize ((b) !! (3 :: Index 8)) :: Unsigned 16)) :: Unsigned 17)) :: Unsigned 18)) :: Unsigned 19) + (resize ((resize ((resize ((resize ((a) !! (4 :: Index 8)) :: Unsigned 16) * (resize ((b) !! (4 :: Index 8)) :: Unsigned 16)) :: Unsigned 17) + (resize ((resize ((a) !! (5 :: Index 8)) :: Unsigned 16) * (resize ((b) !! (5 :: Index 8)) :: Unsigned 16)) :: Unsigned 17)) :: Unsigned 18) + (resize ((resize ((resize ((a) !! (6 :: Index 8)) :: Unsigned 16) * (resize ((b) !! (6 :: Index 8)) :: Unsigned 16)) :: Unsigned 17) + (resize ((resize ((a) !! (7 :: Index 8)) :: Unsigned 16) * (resize ((b) !! (7 :: Index 8)) :: Unsigned 16)) :: Unsigned 17)) :: Unsigned 18)) :: Unsigned 19)

{-# ANN topEntity
  (Synthesize
    { t_name = "DotBuiltin"
    , t_inputs = [PortName "a", PortName "b"]
    , t_output = PortName "y"
    }) #-}
