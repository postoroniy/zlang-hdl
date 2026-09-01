{-# LANGUAGE DataKinds #-}
{-# LANGUAGE NoImplicitPrelude #-}

module FirArchitecture where

import Clash.Prelude

-- ZLang architecture(auto): output=y selected=folded_p2 kind=folded parallelism=2 add_depth=2 equivalence=mathematical
topEntity :: Vec 4 (Unsigned 8) -> Vec 4 (Unsigned 8) -> Unsigned 18
topEntity samples coefficients = (resize ((resize ((resize ((samples) !! (0 :: Index 4)) :: Unsigned 16) * (resize ((coefficients) !! (0 :: Index 4)) :: Unsigned 16)) :: Unsigned 17) + (resize ((resize ((samples) !! (2 :: Index 4)) :: Unsigned 16) * (resize ((coefficients) !! (2 :: Index 4)) :: Unsigned 16)) :: Unsigned 17)) :: Unsigned 18) + (resize ((resize ((resize ((samples) !! (1 :: Index 4)) :: Unsigned 16) * (resize ((coefficients) !! (1 :: Index 4)) :: Unsigned 16)) :: Unsigned 17) + (resize ((resize ((samples) !! (3 :: Index 4)) :: Unsigned 16) * (resize ((coefficients) !! (3 :: Index 4)) :: Unsigned 16)) :: Unsigned 17)) :: Unsigned 18)

{-# ANN topEntity
  (Synthesize
    { t_name = "FirArchitecture"
    , t_inputs = [PortName "samples", PortName "coefficients"]
    , t_output = PortName "y"
    }) #-}
