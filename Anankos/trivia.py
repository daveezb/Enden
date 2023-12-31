import datetime
import csv
import asyncio
import random
import discord
import math
import aiohttp
import mimetypes
import io

class Trivia:
    def __init__(self, client, enabled, channel_id, event_id, role_id, cooldown_min, cooldown_max):
        self.client = client
        self.enabled = enabled
        self.channel_id = channel_id
        self.role_id = role_id
        self.event_id = event_id
        self.cooldown_min = cooldown_min
        self.cooldown_max = cooldown_max
        
        self.current_problemid = -1
        self.last_posted = datetime.datetime(2020, 1, 1)
        self.cooldown_expiration = datetime.datetime(2020, 1, 1)
        self.questions = []

        if self.enabled:
            with open("trivia/" + self.event_id + ".csv") as csvfile:
                reader = csv.reader(csvfile)
                next(reader)
                for row in reader:
                    image = None
                    if len(row) == 7:
                        image = row[6]
                    self.questions.append(self.Question(row[0], row[1], row[2], row[3], row[4], row[5], image))

        self.bg_task = self.client.loop.create_task(self.background_task())

        self.bg_task_qend = None
        self.current_winners = {}
        self.first_answer_timestamp = None

    async def create_tables(self):
        await self.client.db.execute(
            """
            CREATE TABLE IF NOT EXISTS trivia2 (
                eventid VARCHAR,
                userid BIGINT,
                timestamp TIMESTAMP,
                problemid INT,
                points INT,
                UNIQUE(eventid, userid, problemid)
            );
            """
        )
        await self.client.db.execute(
            """
            CREATE TABLE IF NOT EXISTS trivia_config (
                eventid VARCHAR,
                current_problemid INT,
                last_posted TIMESTAMP,
                UNIQUE(eventid)
            );
            """
        )
        await self.client.db.commit()
        if self.enabled:
            cursor = await self.client.db.execute(
                """
                SELECT current_problemid, last_posted FROM trivia_config
                WHERE eventid = ?;
                """,
                (self.event_id, )
            )
            row = await cursor.fetchone()
            if row is None:
                await self.client.db.execute(
                    "INSERT INTO trivia_config (eventid, current_problemid, last_posted) VALUES (?, ?, ?)",
                    (self.event_id, self.current_problemid, self.last_posted)
                )
                await self.client.db.commit()
            else:
                self.current_problemid = row[0]
                self.last_posted = row[1]
                self.cooldown_expiration = self.last_posted + datetime.timedelta(minutes=self.get_random_minutes())
    
    async def on_message(self, message):
        if message.channel.id != self.channel_id or message.author == self.client.user:
            return
        if not self.enabled:
            return
        if message.content.startswith(self.client.cmd_prefix + "top"):
            await self.cmd_top(message)
        if message.content.startswith(self.client.cmd_prefix + "due"):
            await self.cmd_due(message)
        if message.content.startswith(self.client.cmd_prefix + "score"):
            await self.cmd_score(message)
        await self.accept_message_answer(message)

    async def accept_message_answer(self, message):
        answer = message.content
        if await self.question_is_answered():
            # await message.channel.send("This question has already been answered! Please wait for the next question.")
            return
        if self.current_problemid >= len(self.questions):
            return
        question = self.questions[self.current_problemid]
        if question.answer.lower() != answer.lower():
            # await message.channel.send("wrong.")
            return
        if message.author in self.current_winners:
            return
        if self.first_answer_timestamp is None:
            self.first_answer_timestamp = datetime.datetime.now()
        secs_elapsed = (datetime.datetime.now() - self.last_posted).total_seconds()
        points = self.calculate_points(secs_elapsed, question.difficulty)
        if question.bonus:
            points = points + 15
        await self.client.db.execute(
            """
            INSERT INTO trivia2 (eventid, userid, timestamp, problemid, points)
            VALUES (?, ?, ?, ?, ?);
            """,
            (self.event_id, message.author.id, datetime.datetime.now(), self.current_problemid, points)
        )
        await self.client.db.commit()
        self.current_winners[message.author] = points

    async def background_task(self):
        if not self.enabled:
            return
        await self.client.wait_until_ready()
        while not self.client.is_closed():
            if await self.question_is_answered() and datetime.datetime.now() > self.cooldown_expiration:
                await self.post_next_question()
            if self.current_problemid >= len(self.questions):
                await self.client.get_channel(self.channel_id).send("Hey hey <@138881969185357825>, I'm all out of questions! Event over?!")
                return
            await asyncio.sleep(60)

    async def close_answer_bg_task(self):
        while self.first_answer_timestamp == None or (datetime.datetime.now() - self.first_answer_timestamp).total_seconds() < 10:
            await asyncio.sleep(1)
        self.bg_task_qend = None
        self.first_answer_timestamp = None
        question = self.questions[self.current_problemid]
        embed, image_file = await self.get_question_answer_embed()
        winner_fmt = ""
        first = None
        first_pts = 0
        for winner in sorted(self.current_winners, key=self.current_winners.get, reverse=True):
            if first is None:
                first = winner
                first_pts = self.current_winners[winner]
            else:
                winner_fmt = winner_fmt + "\n{} (+{} points)".format(winner.mention, self.current_winners[winner])
        msg = await self.client.get_channel(self.channel_id).send("**{}** got the correct answer, which is **{}**! *(+{} points)*{}".format(first.mention, question.answer, first_pts, winner_fmt), embed=embed, file=image_file)
        await msg.pin()
        self.current_winners = {}

    async def post_next_question(self):
        await self.update_config(
            self.current_problemid + 1,
            datetime.datetime.now(),
            datetime.datetime.now() + datetime.timedelta(minutes=self.get_random_minutes())
        )
        if self.current_problemid >= len(self.questions):
            return
        await self.ask_current_question()

    async def ask_current_question(self):
        self.current_winners = {}
        self.first_answer_timestamp = None
        await self.delete_all_bot_pins()
        embed, image_file = await self.get_question_embed()
        role = self.client.get_channel(self.channel_id).guild.get_role(self.role_id)
        await self.client.get_channel(self.channel_id).send("Look out {}! Next question is dropping in 30 seconds!".format(role.mention))
        self.bg_task_qend = self.client.loop.create_task(self.close_answer_bg_task())
        await asyncio.sleep(30)
        message = await self.client.get_channel(self.channel_id).send(role.mention, embed=embed, file=image_file)
        await message.pin()
        await self.update_config(
            self.current_problemid,
            datetime.datetime.now(),
            datetime.datetime.now() + datetime.timedelta(minutes=self.get_random_minutes())
        )

    async def get_image_file(self, url=None):
        if not url:
            return None
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                buffer = io.BytesIO(await resp.read())
                contentType = resp.headers.get('content-type')
                extension = mimetypes.guess_extension(contentType)
                image_file = discord.File(buffer, "image" + extension)
                return image_file
        return None

    async def get_question_embed(self):
        question = self.questions[self.current_problemid]
        description = "This is a {} question.".format(question.question_type)
        if question.hint and "#" in question.hint:
            description = description + " Please answer according to the provided hint format by filling in the unknowns hashes (#)."
        if question.question_type == "TrueFalse":
            description = "This is a True/False question. Please answer with only true or false."
        embed = discord.Embed(
            title="*{}*".format(question.question),
            description=description
        )
        embed.set_author(name="New Trivia Question:", icon_url="https://i.imgur.com/n13bGeJ.png")
        if question.bonus:
            embed.set_footer(text="Our sources indicates that this is a BONUS QUESTION, which awards more points than usual!", icon_url="https://i.imgur.com/r3kCfzq.png")
        if question.hint:
            embed.add_field(name="Hint:", value=question.hint, inline=True)
        embed.add_field(name="Value", value=question.difficulty, inline=True)
        image_file = None
        if question.image:
            image_file = await self.get_image_file(question.image)
            embed.set_image(url="attachment://" + image_file.filename)
        return embed, image_file

    async def get_question_answer_embed(self):
        question = self.questions[self.current_problemid]
        description = "Please wait for the next question.".format(question.question_type)
        embed = discord.Embed(
            title="*{}*".format(question.question),
            description=description
        )
        embed.set_author(name="Trivia Question Answered!!", icon_url="https://i.imgur.com/nworthx.png")
        if question.bonus:
            embed.set_footer(text="Received extra points as this is a BONUS QUESTION.", icon_url="https://i.imgur.com/r3kCfzq.png")
        embed.add_field(name="Value", value=question.difficulty, inline=True)
        image_file = None
        if question.image:
            image_file = await self.get_image_file(question.image)
            embed.set_image(url="attachment://" + image_file.filename)
        return embed, image_file

    async def delete_all_bot_pins(self):
        pins = list(await self.client.get_channel(self.channel_id).pins())
        for pin in pins:
            if pin.author == self.client.user:
                await pin.unpin()

    async def update_config(self, current_problemid, last_posted, cooldown_expiration):
        self.current_problemid = current_problemid
        self.last_posted = last_posted
        self.cooldown_expiration = cooldown_expiration
        await self.client.db.execute(
            """
            UPDATE trivia_config
            SET current_problemid = ?, last_posted = ?
            WHERE eventid = ?;
            """,
            (self.current_problemid, self.last_posted, self.event_id)
        )
        await self.client.db.commit()

    def get_random_minutes(self):
        return random.randint(self.cooldown_min, self.cooldown_max)

    async def question_is_answered(self):
        if self.current_problemid < 0:
            return True
        if self.bg_task_qend is not None:
            return False
        cursor = await self.client.db.execute(
            "SELECT problemid FROM trivia2 WHERE eventid = ? AND problemid = ?;",
            (self.event_id, self.current_problemid)
        )
        row = await cursor.fetchall()
        return len(row) > 0

    def calculate_points(self, seconds_elapsed, difficulty):
        base_point = [70, 80, 90, 100, 110]
        # 10 second answer get bonus points
        points = (math.cos((math.pi * min(seconds_elapsed, 10)) / 10) + 1) / 2
        points = base_point[difficulty - 1] + round(max(0, points * 10))
        points = points - min(10, len(self.current_winners) * 2)
        return points

    # t-currentTime, b-startvalue, c-changeInValue, d-duration
    # t and d can be frames or secs/millisecs
    # http://gizma.com/easing
    def _easeInOutCirc(self, t, b, c, d):
        if t > d: # past duration, use minimum
            return b + c
        t = t / (d / 2)
        if t < 1:
            return - c / 2 * (math.sqrt(1 - t*t) - 1) + b
        t = t - 2
        return c / 2 * (math.sqrt(1 - t*t) + 1) + b

    def _easeOutQuad(self, t, b, c, d):
        if t > d: # past duration, use minimum
            return b + c
        t = t / d
        return -c * t * (t - 2) + b

    async def get_top_scores(self):
        cursor = await self.client.db.execute(
            """
            SELECT userid, sum(points)
            FROM trivia2
            WHERE eventid = ?
            GROUP BY userid
            ORDER BY sum(points) DESC
            LIMIT 12;
            """,
            (self.event_id, )
        )
        rows = await cursor.fetchall()
        top = []
        for row in rows:
            top.append((row[0], row[1]))
        return top

    async def get_player_score(self, user_id):
        cursor = await self.client.db.execute(
            """
            SELECT sum(points)
            FROM trivia2
            WHERE eventid = ? AND userid = ?
            GROUP BY userid
            """,
            (self.event_id, user_id)
        )
        row = await cursor.fetchone()
        if row is None:
            return 0
        return row[0]

    async def cmd_top(self, message):
        scores = await self.get_top_scores()
        embed = discord.Embed()
        embed.set_author(name="Top Trivia Players", icon_url="https://i.imgur.com/r3kCfzq.png")
        count = 1
        for score in scores:
            user_id = score[0]
            points = score[1]
            user = self.client.get_user(user_id)
            if user:
                embed.add_field(name="{}. {:,}".format(count, points), value=user.mention, inline=True)
                count = count + 1
        await message.channel.send(embed=embed)

    async def cmd_score(self, message):
        score = await self.get_player_score(message.author.id)
        await message.channel.send("{}'s current score is **{:,}**.".format(message.author.mention, score))

    async def cmd_due(self, message):
        if message.author.id == self.client.user.id:
            return
        if not message.author.permissions_in(message.channel).manage_messages:
            return
        seconds_left = (self.cooldown_expiration - datetime.datetime.now()).total_seconds()
        seconds_left = round(max(seconds_left, 0))
        cooldown = self.format_cooldown(seconds_left)
        await message.author.send("Next question will be posted in about {}".format(cooldown))
        await message.delete()
        
    def format_cooldown(self, cooldown):
        if cooldown <= 0:
            return "0s"
        cooldown_tmp = cooldown
        cool_hrs = 0
        cool_mins = 0
        cool_secs = 0
        while cooldown_tmp >= 3600:
            cool_hrs = cool_hrs + 1
            cooldown_tmp = cooldown_tmp - 3600
        while cooldown_tmp >= 60:
            cool_mins = cool_mins + 1
            cooldown_tmp = cooldown_tmp - 60
        cool_secs = cooldown_tmp
        cool_str = ""
        if cool_hrs:
            cool_str = cool_str + " {}h".format(cool_hrs)
        if cool_mins:
            cool_str = cool_str + " {}m".format(cool_mins)
        if cool_secs:
            cool_str = cool_str + " {}s".format(cool_secs)
        cool_str = cool_str.strip()
        return cool_str

    class Question:
        def __init__(self, question, answer, hint, question_type, difficulty, bonus, image=None):
            self.question = str(question).strip()
            self.answer = str(answer).strip()
            self.hint = str(hint).strip()
            self.question_type = str(question_type).strip()
            self.difficulty = int(difficulty)
            self.bonus = True if bonus == "TRUE" else False
            self.image = image if image else None

        def __str__(self):
            return self.question

        def __repr__(self):
            return self.question
