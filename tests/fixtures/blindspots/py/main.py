import pkg.sub.impl as im
from pkg import helper as h
from pkg import run_process


def entry():
    run_process(1)
    h(2)
    im.process(3)
