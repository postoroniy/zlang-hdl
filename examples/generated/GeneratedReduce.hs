{-# LANGUAGE DataKinds #-}
{-# LANGUAGE NoImplicitPrelude #-}

module GeneratedReduce where

import Clash.Prelude

topEntity :: Vec 4 (Unsigned 8) -> Vec 4 (Unsigned 8) -> Unsigned 18
topEntity a b = (resize ((resize ((resize ((a) !! (0 :: Index 4)) :: Unsigned 16) * (resize ((b) !! (0 :: Index 4)) :: Unsigned 16)) :: Unsigned 17) + (resize ((resize ((a) !! (1 :: Index 4)) :: Unsigned 16) * (resize ((b) !! (1 :: Index 4)) :: Unsigned 16)) :: Unsigned 17)) :: Unsigned 18) + (resize ((resize ((resize ((a) !! (2 :: Index 4)) :: Unsigned 16) * (resize ((b) !! (2 :: Index 4)) :: Unsigned 16)) :: Unsigned 17) + (resize ((resize ((a) !! (3 :: Index 4)) :: Unsigned 16) * (resize ((b) !! (3 :: Index 4)) :: Unsigned 16)) :: Unsigned 17)) :: Unsigned 18)

{-# ANN topEntity
  (Synthesize
    { t_name = "GeneratedReduce"
    , t_inputs = [PortName "a", PortName "b"]
    , t_output = PortName "y"
    }) #-}
