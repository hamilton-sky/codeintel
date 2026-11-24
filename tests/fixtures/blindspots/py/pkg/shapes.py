class Circle:
    def __init__(self, r):
        self.r = r

    def area(self):
        return 3.14159 * self.r * self.r


class Square:
    def __init__(self, side):
        self.side = side

    def area(self):
        return self.side * self.side

    def describe(self):
        return f"square of area {self.area()}"


def only_square(sq: Square):
    return sq.area()


def total_area(shapes):
    return sum(s.area() for s in shapes)


def outer():
    def inner():
        return 1

    return inner()


def inner():
    return 2


def calls_module_inner():
    return inner()
