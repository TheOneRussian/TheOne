from pychess.System.Log import log
from pychess.Utils.GameModel import GameModel
from pychess.Utils.Offer import Offer
from pychess.Utils.const import *
from pychess.Players.Human import Human
from pychess.ic import GAME_TYPES

class ICGameModel (GameModel):
    def __init__ (self, connection, ficsgame, timemodel):
        assert ficsgame.game_type in GAME_TYPES.values()
        GameModel.__init__(self, timemodel, ficsgame.game_type.variant)
        self.connection = connection
        self.ficsgame = ficsgame
        
        connections = self.connections
        connections[connection.bm].append(connection.bm.connect("boardUpdate", self.onBoardUpdate))
        connections[connection.bm].append(connection.bm.connect("obsGameEnded", self.onGameEnded))
        connections[connection.bm].append(connection.bm.connect("curGameEnded", self.onGameEnded))
        connections[connection.bm].append(connection.bm.connect("gamePaused", self.onGamePaused))
        connections[connection.om].append(connection.om.connect("onActionError", self.onActionError))
        connections[connection].append(connection.connect("disconnected", self.onDisconnected))
        
        rated = "rated" if ficsgame.rated else "unrated"
        # This is in the format that ficsgame.com writes these PGN headers
        self.tags["Event"] = "FICS %s %s game" % (rated, ficsgame.game_type.fics_name)
        self.tags["Site"] = "FICS"

    def __repr__ (self):
        s = GameModel.__repr__(self)
        s = s.replace("<GameModel", "<ICGameModel")
        s = s.replace(", players=", ", ficsgame=%s, players=" % self.ficsgame)
        return s
    
    def __disconnect (self):
        if self.connections is None: return
        for obj in self.connections:
            # Humans need to stay connected post-game so that "GUI > Actions" works
            if isinstance(obj, Human):
                continue
            
            for handler_id in self.connections[obj]:
                if obj.handler_is_connected(handler_id):
                    log.debug("ICGameModel.__disconnect: object=%s handler_id=%s\n" % \
                        (repr(obj), repr(handler_id)))
                    obj.disconnect(handler_id)
        self.connections = None
    
    def hasGuestPlayers (self):
        for player in (self.ficsgame.wplayer, self.ficsgame.bplayer):
            if player.isGuest():
                return True
        return False
    
    def onBoardUpdate (self, bm, gameno, ply, curcol, lastmove, fen, wname, bname, wms, bms):
        log.debug(("ICGameModel.onBoardUpdate: id=%s self.ply=%s self.players=%s gameno=%s " + \
                  "wname=%s bname=%s ply=%s curcol=%s lastmove=%s fen=%s wms=%s bms=%s\n") % \
                  (str(id(self)), str(self.ply), repr(self.players), str(gameno), str(wname), str(bname), \
                   str(ply), str(curcol), str(lastmove), str(fen), str(wms), str(bms)))
        if gameno != self.ficsgame.gameno or len(self.players) < 2 or wname != self.players[0].ichandle \
           or bname != self.players[1].ichandle:
            return
        log.debug("ICGameModel.onBoardUpdate: id=%d, self.players=%s: updating time and/or ply\n" % \
            (id(self), str(self.players)))
        
        if self.timemodel:
            log.debug("ICGameModel.onBoardUpdate: id=%d self.players=%s: updating timemodel\n" % \
                (id(self), str(self.players)))
            self.timemodel.updatePlayer (WHITE, wms/1000.)
            self.timemodel.updatePlayer (BLACK, bms/1000.)
        
        if ply < self.ply:
            log.debug("ICGameModel.onBoardUpdate: id=%d self.players=%s self.ply=%d ply=%d: TAKEBACK\n" % \
                (id(self), str(self.players), self.ply, ply))
            offers = self.offers.keys()
            for offer in offers:
                if offer.type == TAKEBACK_OFFER:
                    # There can only be 1 outstanding takeback offer for both players on FICS,
                    # (a counter-offer by the offeree for a takeback for a different number of
                    # moves replaces the initial offer) so we can safely remove all of them
                    del self.offers[offer]
            self.undoMoves(self.ply-ply)
    
    def onGameEnded (self, bm, ficsgame):
        if ficsgame == self.ficsgame and len(self.players) >= 2:
            log.debug(
                "ICGameModel.onGameEnded: self.players=%s ficsgame=%s\n" % \
                (repr(self.players), repr(ficsgame)))
            self.end(ficsgame.result, ficsgame.reason)
    
    def setPlayers (self, players):
        GameModel.setPlayers(self, players)
        if self.players[WHITE].icrating:
            self.tags["WhiteElo"] = self.players[WHITE].icrating
        if self.players[BLACK].icrating:
            self.tags["BlackElo"] = self.players[BLACK].icrating
    
    def onGamePaused (self, bm, gameno, paused):
        if paused:
            self.pause()
        else: self.resume()
        
        # we have to do this here rather than in acceptRecieved(), because
        # sometimes FICS pauses/unpauses a game clock without telling us that the
        # original offer was "accepted"/"received", such as when one player offers
        # "pause" and the other player responds not with "accept" but "pause"
        for offer in self.offers.keys():
            if offer.type in (PAUSE_OFFER, RESUME_OFFER):
                del self.offers[offer]
    
    def onDisconnected (self, connection):
        if self.status in (WAITING_TO_START, PAUSED, RUNNING):
            self.end (KILLED, DISCONNECTED)
    
    ############################################################################
    # Offer management                                                         #
    ############################################################################
    
    def offerRecieved (self, player, offer):
        log.debug("ICGameModel.offerRecieved: offerer=%s %s\n" % (repr(player), offer))
        if player == self.players[WHITE]:
            opPlayer = self.players[BLACK]
        else: opPlayer = self.players[WHITE]
        
        if offer.type == CHAT_ACTION:
            opPlayer.putMessage(offer.param)
        
        elif offer.type in (RESIGNATION, FLAG_CALL):
            self.connection.om.offer(offer, self.ply)
        
        elif offer.type in OFFERS:
            if offer not in self.offers:
                log.debug("ICGameModel.offerRecieved: %s.offer(%s)\n" % (repr(opPlayer), offer))
                self.offers[offer] = player
                opPlayer.offer(offer)
            # If the offer was an update to an old one, like a new takebackvalue
            # we want to remove the old one from self.offers
            for offer_ in self.offers.keys():
                if offer.type == offer_.type and offer != offer_:
                    del self.offers[offer_]
    
    def acceptRecieved (self, player, offer):
        log.debug("ICGameModel.acceptRecieved: accepter=%s %s\n" % (repr(player), offer))
        if player.__type__ == LOCAL:
            if offer not in self.offers or self.offers[offer] == player:
                player.offerError(offer, ACTION_ERROR_NONE_TO_ACCEPT)
            else:
                log.debug("ICGameModel.acceptRecieved: connection.om.accept(%s)\n" % offer)
                self.connection.om.accept(offer)
                del self.offers[offer]
        
        # We don't handle any ServerPlayer calls here, as the fics server will
        # know automatically if he/she accepts an offer, and will simply send
        # us the result.
    
    def checkStatus (self):
        pass

    def onActionError (self, om, offer, error):
        self.emit("action_error", offer, error)
    
    #
    # End
    #
    
    def end (self, status, reason):
        if self.status in UNFINISHED_STATES:
            self.__disconnect()
            
            if self.isObservationGame():
                self.connection.bm.unobserve(self.ficsgame)
            else:
                self.connection.om.offer(Offer(ABORT_OFFER), -1)
                self.connection.om.offer(Offer(RESIGNATION), -1)
        
        GameModel.end(self, status, reason)
