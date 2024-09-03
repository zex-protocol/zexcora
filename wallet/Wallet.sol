// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";

interface ISchnorrVerifier {
    function verifySignature(
        uint256 pubKeyX,
        uint8 pubKeyYParity,
        uint256 signature,
        uint256 msgHash,
        address nonceTimesGeneratorAddress
    ) external view returns (bool);
}

contract Wallet is Ownable {
    uint256 public pubKeyX;
    uint8 public pubKeyYParity;
    ISchnorrVerifier public verifier;
    mapping(uint256 => uint256) public nonces;
    uint256[] public users;
    mapping(address => uint256) public tokenToIndex;
    mapping(uint256 => address) public indexToToken;
    uint256 public nextTokenIndex = 1;

    event Deposit(uint256 indexed tokenIndex, uint256 amount, uint256 indexed pubKeyX, uint8 indexed pubKeyYParity);
    event Withdrawal(address indexed token, address indexed to, uint256 amount);
    event PublicKeySet(uint256 indexed pubKeyX, uint8 indexed pubKeyYParity);
    event WithdrawNonceReset();

    constructor(address _verifier, uint256 _pubKeyX, uint8 _pubKeyYParity) Ownable(msg.sender) {
        verifier = ISchnorrVerifier(_verifier);
        pubKeyX = _pubKeyX;
        pubKeyYParity = _pubKeyYParity;
    }

    function setVerifier(address _verifier) external onlyOwner {
        verifier = ISchnorrVerifier(_verifier);
    }

    function setPublicKey(uint256 _pubKeyX, uint8 _pubKeyYParity) external onlyOwner {
        pubKeyX = _pubKeyX;
        pubKeyYParity = _pubKeyYParity;
        emit PublicKeySet(pubKeyX, pubKeyYParity);
    }

    function deposit(address token, uint256 amount, uint256 _publicKeyX, uint8 _pubKeyYParity) external {
        require(amount > 0, "Amount must be greater than 0");

        uint256 tokenIndex = tokenToIndex[token];
        if (tokenIndex == 0) {
            tokenIndex = nextTokenIndex++;
            tokenToIndex[token] = tokenIndex;
            indexToToken[tokenIndex] = token; // Update reverse lookup mapping
        }

        bool sent = IERC20(token).transferFrom(msg.sender, address(this), amount);
        require(sent, "Token transfer failed");
        emit Deposit(tokenIndex, amount, _publicKeyX, _pubKeyYParity);
    }

    // Function to add or update a nonce for a user
    function setNonce(uint256 userId, uint256 nonce) public {
        if (nonces[userId] == 0 && nonce != 0) {
            users.push(userId);
        }
        nonces[userId] = nonce;
    }

    // Function to get the current nonce for a user
    function getNonce(uint256 userId) public view returns (uint256) {
        return nonces[userId];
    }

    //////////////// Warning /////////////////
    // Function to reset all nonces to 0 /////
    //////// This is only for test ///////////
    function resetAllNonces() public {
        for (uint i = 0; i < users.length; i++) {
            nonces[users[i]] = 0;
        }
        delete users;
    }

    function withdraw(
        uint256 tokenIndex,
        uint256 amount,
        uint256 user,
        address dest,
        uint256 nonce,
        uint256 signature,
        address nonceTimesGeneratorAddress
    ) external {
        require(nonce == getNonce(user), "Invalid nonce");
        uint256 msgHash = uint256(keccak256(abi.encodePacked(user, dest, tokenIndex, amount, nonce)));

        require(
            verifier.verifySignature(pubKeyX, pubKeyYParity, signature, msgHash, nonceTimesGeneratorAddress),
            "Invalid signature"
        );

        setNonce(user, nonce + 1);

        address token = indexToToken[tokenIndex];
        bool sent = IERC20(token).transfer(dest, amount);
        require(sent, "Token transfer failed");
        emit Withdrawal(token, dest, amount);
    }
}