{-# LANGUAGE DataKinds #-}
{-# LANGUAGE NoImplicitPrelude #-}

module ALU where

import Clash.Prelude

topEntity :: Unsigned 32 -> Unsigned 32 -> Unsigned 3 -> Unsigned 32
topEntity a b op = case op of { 0 -> (resize ((resize (a) :: Unsigned 33) + (resize (b) :: Unsigned 33)) :: Unsigned 32); 1 -> (a) - (b); 2 -> (a) .&. (b); 3 -> (a) .|. (b); _ -> (0 :: Unsigned 32) }

{-# ANN topEntity
  (Synthesize
    { t_name = "ALU"
    , t_inputs = [PortName "a", PortName "b", PortName "op"]
    , t_output = PortName "y"
    }) #-}
