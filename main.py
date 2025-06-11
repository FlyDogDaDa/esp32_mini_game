import uasyncio as asyncio
from machine import Pin, PWM
import tm1637
import urandom as random
import time


class SimpleQueue:
    def __init__(self):
        self._queue = []
        self._ev = asyncio.Event()

    def qsize(self):
        return len(self._queue)

    def empty(self):
        return len(self._queue) == 0

    async def put(self, item):
        self._queue.append(item)
        self._ev.set()  # 通知有新項目

    async def get(self):
        while self.empty():
            await self._ev.wait()  # 等待新項目事件

        item = self._queue.pop(0)
        if self.empty():
            self._ev.clear()  # 佇列空了，清除事件標誌
        return item


class AsyncLooping:
    def __init__(self):
        self.loop_runing = False
        self.loop_task: asyncio.Task | None = None

    async def loop(self):
        raise NotImplementedError

    def start(self):
        self.loop_runing = True
        self.loop_task = asyncio.create_task(self.loop())

    async def stop(self):
        self.loop_runing = False
        if self.loop_task is not None:
            await self.loop_task


class Button(Pin, AsyncLooping):
    def __init__(self, pin_number: int):
        super().__init__(pin_number, Pin.IN)
        self.state = False
        self.on_pressed = None
        self.on_released = None

    @property
    def is_pressed(self) -> bool:
        return not self.value()

    async def loop(self):
        while self.loop_runing:
            await asyncio.sleep(0.01)
            is_pressed = self.is_pressed
            # 有無變動
            if not is_pressed ^ self.state:
                continue
            self.state = is_pressed
            # 觸發事件
            if is_pressed:
                if self.on_pressed is not None:
                    await self.on_pressed()
            else:
                if self.on_released is not None:
                    await self.on_released()

    def set_on_pressed(self, callback) -> "Button":
        self.on_pressed = callback
        return self

    def set_on_released(self, callback) -> "Button":
        self.on_released = callback
        return self


class Buzzer(PWM):
    def __init__(self, pin_number: int):
        super().__init__(Pin(pin_number, Pin.OUT))
        self.freq(0)
        self.duty(16000)

    async def play(self, time: float, freq: int = 0):
        self.freq(freq)
        await asyncio.sleep(time)
        self.freq(0)


class Led(Pin):
    def __init__(self, pin_number: int):
        super().__init__(pin_number, Pin.OUT)

    def on(self):
        self.value(1)

    def off(self):
        self.value(0)

    def toggle(self):
        self.value(not self.value())

    @property
    def is_on(self) -> bool:
        return bool(self.value())


class DigitalDisplay(tm1637.TM1637, AsyncLooping):
    def __init__(self, clk_pin: int, dio_pin: int):
        tm1637.TM1637.__init__(self, Pin(clk_pin), Pin(dio_pin))
        AsyncLooping.__init__(self)
        self.minute = 0
        self.second = 0
        self.timeup_callback = None
        self.is_pause = False
        self.is_show = True

    def pause(self):
        self.is_pause = True

    def set_timeup_callback(self, callback):
        self.timeup_callback = callback

    async def loop(self):
        while self.loop_runing:
            if self.is_pause:
                await asyncio.sleep(0.5)
                self.is_show = not self.is_show
                if self.is_show:
                    self.numbers(self.minute, self.second)
                else:
                    self.write([0b00000000, 0b00000000, 0b00000000, 0b00000000])
                continue
            self.second -= 1
            # 秒借位
            if self.second < 0:
                self.second = 59
                self.minute -= 1
            # 分鐘被借光歸零
            if self.minute < 0:
                self.minute = 0
            # 更新顯示
            self.numbers(self.minute, self.second)
            await asyncio.sleep(1)
            # 時間到
            if self.second == 0 and self.minute == 0:
                # 停止迴圈
                self.loop_runing = False
                # 觸發「時間到」事件
                if self.timeup_callback is not None:
                    await self.timeup_callback()

    def set_time(self, second: int, minute: int = 0):
        quotient, second = divmod(second, 60)
        minute += quotient
        self.second = second
        self.minute = minute


# led = Led(5)
buzzer = Buzzer(5)
digital_display = DigitalDisplay(clk_pin=2, dio_pin=0)
up_left_button = Button(17)
up_right_button = Button(12)
down_left_button = Button(16)
down_right_button = Button(14)
up_left_button.start()
up_right_button.start()
down_left_button.start()
down_right_button.start()
transfer_queue = SimpleQueue()
play_task: asyncio.Task | None = None


async def death_sound():
    await buzzer.play(2.0, 980)
    await buzzer.play(1, 64)
    await asyncio.sleep(0.5)
    for hz in range(512, 64, -32):
        await buzzer.play(0.1, hz)


async def win_sound():
    for hz in range(150, 720, 32):
        await buzzer.play(0.03, hz)
    for _ in range(3):
        await asyncio.sleep(0.05)
        await buzzer.play(0.2, 760)
    await buzzer.play(1, 860)


async def game_over():
    if play_task is not None:
        play_task.cancel()
    digital_display.write([0b11111111, 0b11111111, 0b11111111, 0b11111111])
    await up_left_button.stop()
    await up_right_button.stop()
    await down_left_button.stop()
    await down_right_button.stop()
    await digital_display.stop()
    await death_sound()
    digital_display.write([0b00000000, 0b00000000, 0b00000000, 0b00000000])


async def game_win():
    if play_task is not None:
        play_task.cancel()
    await up_left_button.stop()
    await up_right_button.stop()
    await down_left_button.stop()
    await down_right_button.stop()
    # 暫停數字顯示器(閃爍)
    digital_display.pause()
    await win_sound()
    # 卡住10分鐘
    await asyncio.sleep(600)


async def transfer_to_win():
    await transfer_queue.put(game_win())


async def transfer_to_lose():
    await transfer_queue.put(game_over())


async def play_morse(tape: list[int]):
    try:
        while True:
            for i in tape:
                if i == 0:
                    await asyncio.sleep(0.25)
                    await buzzer.play(0.25, 548)
                    await asyncio.sleep(0.25)
                else:
                    await buzzer.play(1, 548)
                await asyncio.sleep(0.1)
            await asyncio.sleep(1.5)
    except asyncio.CancelledError:
        pass


async def play_pitch(tape: list[int]):
    try:
        while True:
            for i in tape:
                if i == 0:
                    await buzzer.play(0.5, 368)
                else:
                    await buzzer.play(0.5, 762)
                await asyncio.sleep(0.1)
            await asyncio.sleep(1.5)
    except asyncio.CancelledError:
        pass


async def morse():  # 長短音
    global play_task
    # 0是短，1是長
    mode = random.randint(0, 1)
    # 隨機資料(4隨機，其中最少1)
    code = [random.randint(0, 1) for _ in range(4)]
    code[random.randint(0, 3)] = 1  # 至少一個為1
    tape = [mode, 1] + code
    play_task = asyncio.create_task(play_morse(tape))

    # ==短長==
    if mode == 0:
        set_all_buttons_with(transfer_to_lose)
        count = sum(code) - 1
        b = [
            up_left_button,
            down_right_button,
            up_right_button,
            down_left_button,
        ][count]
        b.set_on_pressed(transfer_to_win)

    # ==長長==
    if mode == 1:
        for long, b in zip(
            code,
            [
                up_left_button,
                down_right_button,
                up_right_button,
                down_left_button,
            ],
        ):
            # i是長短，b是按鈕
            set_all_buttons_with(transfer_to_lose)
            time_queue = SimpleQueue()

            async def record_press_time():
                await time_queue.put((time.time(), "press"))

            async def record_release_time():
                await time_queue.put((time.time(), "release"))

            # 記錄按下與放開時間
            b.set_on_pressed(record_press_time).set_on_released(record_release_time)
            # 等待直到任意按鈕
            while time_queue.qsize() < 2 and transfer_queue.empty():
                # 等待任意按鈕
                await asyncio.sleep(0.05)
            # 有轉移(按錯按鈕)
            if not transfer_queue.empty():
                print("跳出!!")
                return
            # 上一關還沒放開
            if time_queue._queue[0][1] == "release":
                # 清掉遺存
                await time_queue.get()
            # 按對按鈕
            press_time, _ = await time_queue.get()
            release_time, _ = await time_queue.get()
            hold_time = abs(release_time - press_time)
            is_hold_long = hold_time > 0.5
            if long ^ is_hold_long:
                # 按壓時間不同
                await transfer_to_lose()
            else:
                # 按壓時間相同
                continue
        await transfer_to_win()


async def pitch():  # 音高
    global play_task
    # 0是低音，1是高音
    mode = random.randint(0, 1)
    # 0短1長按
    long = random.randint(0, 1)
    # 0下1上
    up = random.randint(0, 1)
    # 0左1右
    right = random.randint(0, 1)
    tape = [mode, mode ^ 1, long, up, right]
    play_task = asyncio.create_task(play_pitch(tape))

    set_all_buttons_with(transfer_to_lose)
    # ==低高==
    if mode == 0:  # 記錄按下與放開時間
        b = [
            [down_left_button, down_right_button],
            [up_left_button, up_right_button],
        ][
            up
        ][right]
        time_queue = SimpleQueue()

        async def record_press_time():
            await time_queue.put((time.time(), "press"))

        async def record_release_time():
            await time_queue.put((time.time(), "release"))

        b.set_on_pressed(record_press_time).set_on_released(record_release_time)
        # 等待直到任意按鈕
        while time_queue.qsize() < 2 and transfer_queue.empty():
            await asyncio.sleep(0.05)
        # 上一關還沒放開
        if time_queue._queue[0][1] == "release":
            # 清掉遺存
            await time_queue.get()
        # 有轉移(按錯按鈕)
        if not transfer_queue.empty():
            return
        # 按對按鈕
        press_time, _ = await time_queue.get()
        release_time, _ = await time_queue.get()
        hold_time = release_time - press_time
        is_hold_long = hold_time > 0.5
        if long ^ is_hold_long:
            # 按壓時間不同
            await transfer_to_lose()
        else:
            # 按壓時間相同
            await transfer_to_win()
    # ==高低==
    if mode == 1:
        b = [down_left_button, up_right_button][right]
        b.set_on_pressed(transfer_to_win)


async def game(time: int = 60 * 3):
    digital_display.set_timeup_callback(transfer_to_lose)
    digital_display.set_time(time)
    digital_display.start()
    # 隨機遊戲模組
    if random.randint(0, 1):
        await transfer_queue.put(morse())
    else:
        await transfer_queue.put(pitch())


def set_all_buttons_with(func):
    up_left_button.set_on_pressed(func)
    up_right_button.set_on_pressed(func)
    down_left_button.set_on_pressed(func)
    down_right_button.set_on_pressed(func)


async def main():
    set_all_buttons_with(transfer_to_lose)
    position_for_start = random.randint(0, 3)
    t, b = [
        ([0b01100011, 0b00000000, 0b00000000, 0b00000000], up_left_button),
        ([0b00000000, 0b00000000, 0b00000000, 0b01100011], up_right_button),
        ([0b01011100, 0b00000000, 0b00000000, 0b00000000], down_left_button),
        ([0b00000000, 0b00000000, 0b00000000, 0b01011100], down_right_button),
    ][position_for_start]
    digital_display.write(t)

    async def transfer_to_game():
        await transfer_queue.put(game())

    b.set_on_pressed(transfer_to_game)

    # 跑主要線路
    while True:
        print("嘗試拿")
        transfer = await transfer_queue.get()
        print("拿到")
        await transfer


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        buzzer.deinit()
