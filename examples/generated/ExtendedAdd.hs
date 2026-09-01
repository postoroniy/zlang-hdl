{-# LANGUAGE DataKinds #-}
{-# LANGUAGE NoImplicitPrelude #-}

module ExtendedAdd where

import Clash.Prelude

topEntity :: Unsigned 8 -> Unsigned 8 -> Unsigned 16
topEntity a b = (resize ((resize (a) :: Unsigned 9) + (resize (b) :: Unsigned 9)) :: Unsigned 16)

{-# ANN topEntity
  (Synthesize
    { t_name = "ExtendedAdd"
    , t_inputs = [PortName "a", PortName "b"]
    , t_output = PortName "y"
    }) #-}
