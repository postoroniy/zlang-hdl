{-# LANGUAGE DataKinds #-}
{-# LANGUAGE NoImplicitPrelude #-}

module Add where

import Clash.Prelude

topEntity :: Unsigned 8 -> Unsigned 8 -> Unsigned 9
topEntity a b = (resize (a) :: Unsigned 9) + (resize (b) :: Unsigned 9)

{-# ANN topEntity
  (Synthesize
    { t_name = "Add"
    , t_inputs = [PortName "a", PortName "b"]
    , t_output = PortName "y"
    }) #-}
