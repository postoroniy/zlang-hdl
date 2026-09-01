{-# LANGUAGE DataKinds #-}
{-# LANGUAGE NoImplicitPrelude #-}

module MappedSum where

import Clash.Prelude

topEntity :: Vec 4 (Unsigned 8) -> Unsigned 11
topEntity values = (resize ((resize ((resize ((values) !! (0 :: Index 4)) :: Unsigned 9) + (resize ((values) !! (0 :: Index 4)) :: Unsigned 9)) :: Unsigned 10) + (resize ((resize ((values) !! (1 :: Index 4)) :: Unsigned 9) + (resize ((values) !! (1 :: Index 4)) :: Unsigned 9)) :: Unsigned 10)) :: Unsigned 11) + (resize ((resize ((resize ((values) !! (2 :: Index 4)) :: Unsigned 9) + (resize ((values) !! (2 :: Index 4)) :: Unsigned 9)) :: Unsigned 10) + (resize ((resize ((values) !! (3 :: Index 4)) :: Unsigned 9) + (resize ((values) !! (3 :: Index 4)) :: Unsigned 9)) :: Unsigned 10)) :: Unsigned 11)

{-# ANN topEntity
  (Synthesize
    { t_name = "MappedSum"
    , t_inputs = [PortName "values"]
    , t_output = PortName "y"
    }) #-}
